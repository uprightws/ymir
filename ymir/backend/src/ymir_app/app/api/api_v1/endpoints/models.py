import enum
import os
from typing import Dict, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.logger import logger
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.api import deps
from app.api.errors.errors import (
    ModelNotFound,
    DuplicateModelGroupError,
    FailedtoImportModel,
    TaskNotFound,
    FieldValidationFailed,
)
from app.constants.state import TaskState, TaskType, ResultState
from app.utils.files import NGINX_DATA_PATH
from app.utils.ymir_controller import gen_user_hash, gen_repo_hash, ControllerClient

router = APIRouter()


@router.get(
    "/batch",
    response_model=schemas.ModelsOut,
)
def batch_get_models(
    db: Session = Depends(deps.get_db),
    model_ids: str = Query(None, alias="ids"),
) -> Any:
    ids = [int(i) for i in model_ids.split(",")]
    models = crud.model.get_multi_by_ids(db, ids=ids)
    return {"result": models}


class SortField(enum.Enum):
    id = "id"
    create_datetime = "create_datetime"
    map = "map"
    source = "source"


@router.get("/", response_model=schemas.ModelPaginationOut)
def list_models(
    db: Session = Depends(deps.get_db),
    source: TaskType = Query(None, description="type of related task"),
    state: ResultState = Query(None),
    project_id: int = Query(None),
    group_id: int = Query(None),
    training_dataset_id: int = Query(None),
    offset: int = Query(None),
    limit: int = Query(None),
    order_by: SortField = Query(SortField.id),
    is_desc: bool = Query(True),
    start_time: int = Query(None, description="from this timestamp"),
    end_time: int = Query(None, description="to this timestamp"),
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get list of models

    pagination is supported by means of offset and limit

    filters:
    - state
    - project_id
    - start_time, end_time
    """
    models, total = crud.model.get_multi_models(
        db,
        user_id=current_user.id,
        project_id=project_id,
        group_id=group_id,
        source=source,
        state=state,
        offset=offset,
        limit=limit,
        order_by=order_by.name,
        is_desc=is_desc,
        start_time=start_time,
        end_time=end_time,
    )
    payload = {"total": total, "items": models}
    return {"result": payload}


@router.post(
    "/importing",
    response_model=schemas.ModelOut,
)
def import_model(
    *,
    db: Session = Depends(deps.get_db),
    model_import: schemas.ModelImport,
    current_user: models.User = Depends(deps.get_current_active_user),
    controller_client: ControllerClient = Depends(deps.get_controller_client),
    background_tasks: BackgroundTasks,
) -> Any:

    # 1. validation model group name
    if crud.model_group.is_duplicated_name_in_project(
        db=db, project_id=model_import.project_id, name=model_import.group_name
    ):
        raise DuplicateModelGroupError()

    # 2. create placeholder task
    if model_import.import_type is None:
        raise FailedtoImportModel
    task = crud.task.create_placeholder(
        db=db,
        type_=model_import.import_type,
        state_=TaskState.pending,
        user_id=current_user.id,
        project_id=model_import.project_id,
    )
    logger.info("[import model] related task created: %s", task.hash)

    # 3. create model group
    model_group_in = schemas.ModelGroupCreate(
        name=model_import.group_name,
        project_id=model_import.project_id,
        description=model_import.description,
    )
    model_group = crud.model_group.create_with_user_id(db=db, user_id=current_user.id, obj_in=model_group_in)

    # 4. create model record
    model_in = schemas.ModelCreate(
        description=model_import.description,
        source=task.type,
        result_state=ResultState.processing,
        model_group_id=model_group.id,
        project_id=model_import.project_id,
        user_id=current_user.id,
        task_id=task.id,
    )
    model = crud.model.create_with_version(db=db, obj_in=model_in, dest_group_name=model_import.group_name)
    logger.info("[import model] model record created: %s", model)

    # 5. run background task
    background_tasks.add_task(
        import_model_in_background,
        db,
        controller_client,
        model_import,
        current_user.id,
        task.hash,
        model.id,
    )
    return {"result": model}


def create_model_record(db: Session, model_import: schemas.ModelImport, task: models.Task) -> models.Model:
    """
    bind task info to model record
    """
    model_info = jsonable_encoder(model_import)
    model_info.update(
        {
            "hash": task.hash,
            "task_id": task.id,
            "user_id": task.user_id,
        }
    )
    return crud.model.create(db, obj_in=schemas.ModelCreate(**model_info))


def import_model_in_background(
    db: Session,
    controller_client: ControllerClient,
    model_import: schemas.ModelImport,
    user_id: int,
    task_hash: str,
    model_id: int,
) -> None:
    try:
        _import_model(db, controller_client, model_import, user_id, task_hash)
    except (ValueError, FieldValidationFailed, FailedtoImportModel, ModelNotFound, TaskNotFound):
        logger.exception("[import model] failed to import model, set model result_state to error")
        crud.model.update_state(db, model_id=model_id, new_state=ResultState.error)


def _import_model(
    db: Session, controller_client: ControllerClient, model_import: schemas.ModelImport, user_id: int, task_hash: str
) -> None:
    logger.info(
        "[import model] start importing model file from %s",
        model_import,
    )
    parameters: Dict[str, Any] = {}
    if model_import.import_type == TaskType.copy_model:
        # get the task.hash from input_model
        model_obj = crud.model.get(db, id=model_import.input_model_id)
        if model_obj is None:
            raise ModelNotFound()
        task_obj = crud.task.get(db, id=model_obj.task_id)
        if task_obj is None:
            raise TaskNotFound()
        parameters = {
            "src_user_id": gen_user_hash(model_obj.user_id),
            "src_repo_id": gen_repo_hash(model_obj.project_id),
            "src_resource_id": task_obj.hash,
        }
    elif model_import.import_type == TaskType.import_model and model_import.input_model_path is not None:
        # TODO(chao): remove model file after importing
        parameters = {
            "model_package_path": os.path.join(NGINX_DATA_PATH, model_import.input_model_path),
        }
    else:
        raise FieldValidationFailed()

    try:
        controller_client.import_model(
            user_id=user_id,
            project_id=model_import.project_id,
            task_id=task_hash,
            task_type=model_import.import_type,
            args=parameters,
        )
    except ValueError as e:
        logger.exception("[import model] controller error: %s", e)
        raise FailedtoImportModel()


@router.delete(
    "/{model_id}",
    response_model=schemas.ModelOut,
    responses={
        400: {"description": "No permission"},
        404: {"description": "Model Not Found"},
    },
)
def delete_model(
    *,
    db: Session = Depends(deps.get_db),
    model_id: int = Path(..., example="12"),
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Delete model
    (soft delete actually)
    """
    model = crud.model.get_by_user_and_id(db, user_id=current_user.id, id=model_id)
    if not model:
        raise ModelNotFound()

    model = crud.model.soft_remove(db, id=model_id)
    return {"result": model}


@router.get(
    "/{model_id}",
    response_model=schemas.ModelOut,
    responses={404: {"description": "Model Not Found"}},
)
def get_model(
    db: Session = Depends(deps.get_db),
    model_id: int = Path(..., example="12"),
    current_user: models.User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get verbose information of specific model
    """
    model = crud.model.get_by_user_and_id(db, user_id=current_user.id, id=model_id)
    if not model:
        raise ModelNotFound()
    return {"result": model}