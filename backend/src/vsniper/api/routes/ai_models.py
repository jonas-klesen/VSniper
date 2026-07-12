from fastapi import APIRouter, HTTPException, status

from vsniper.domain.contracts import AiModelConfig, AiModelCreate, AiModelUpdate
from vsniper.services import ai_model_service

router = APIRouter(tags=["ai-models"])


@router.get("/ai-models", response_model=list[AiModelConfig])
def list_ai_models() -> list[AiModelConfig]:
    return ai_model_service.list_models()


@router.post("/ai-models", response_model=AiModelConfig, status_code=status.HTTP_201_CREATED)
def create_ai_model(payload: AiModelCreate) -> AiModelConfig:
    return ai_model_service.create_model(payload)


@router.put("/ai-models/{model_id}", response_model=AiModelConfig)
def update_ai_model(model_id: str, payload: AiModelUpdate) -> AiModelConfig:
    try:
        return ai_model_service.update_model(model_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI model not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/ai-models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ai_model(model_id: str) -> None:
    try:
        ai_model_service.delete_model(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI model not found") from exc
    except ai_model_service.AiModelInUse as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="AI model is still referenced by Settings; unassign it before deleting.",
        ) from exc
