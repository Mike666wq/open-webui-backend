import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from open_webui.config import (
    AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH,
    DEFAULT_AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_FOLLOW_UP_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_TAGS_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE,
    DEFAULT_VOICE_MODE_PROMPT_TEMPLATE,
)
from open_webui.constants import ERROR_MESSAGES, TASKS
from open_webui.models.config import Config
from open_webui.routers.pipelines import process_pipeline_inlet_filter
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.models import get_request_models, load_provider_models
from open_webui.utils.payload import apply_params_to_form_data
from open_webui.utils.task import (
    autocomplete_generation_template,
    emoji_generation_template,
    follow_up_generation_template,
    get_task_model_id,
    image_prompt_generation_template,
    moa_response_generation_template,
    query_generation_template,
    tags_generation_template,
    title_generation_template,
)
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()

TASK_CONFIG_KEYS = {
    'TASK_MODEL': 'task.model.default',
    'TASK_MODEL_EXTERNAL': 'task.model.external',
    'TASK_MODEL_PARAMS': 'task.model.params',
    'TASK_MODEL_TITLE': 'task.model.title.default',
    'TASK_MODEL_TITLE_EXTERNAL': 'task.model.title.external',
    'TASK_MODEL_TAGS': 'task.model.tags.default',
    'TASK_MODEL_TAGS_EXTERNAL': 'task.model.tags.external',
    'TASK_MODEL_FOLLOWUP': 'task.model.followup.default',
    'TASK_MODEL_FOLLOWUP_EXTERNAL': 'task.model.followup.external',
    'TASK_MODEL_QUERY': 'task.model.query.default',
    'TASK_MODEL_QUERY_EXTERNAL': 'task.model.query.external',
    'TITLE_GENERATION_PROMPT_TEMPLATE': 'task.title.prompt_template',
    'IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE': 'task.image.prompt_template',
    'ENABLE_AUTOCOMPLETE_GENERATION': 'task.autocomplete.enable',
    'AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH': 'task.autocomplete.input_max_length',
    'AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE': 'task.autocomplete.prompt_template',
    'TAGS_GENERATION_PROMPT_TEMPLATE': 'task.tags.prompt_template',
    'FOLLOW_UP_GENERATION_PROMPT_TEMPLATE': 'task.follow_up.prompt_template',
    'ENABLE_FOLLOW_UP_GENERATION': 'task.follow_up.enable',
    'ENABLE_TAGS_GENERATION': 'task.tags.enable',
    'ENABLE_TITLE_GENERATION': 'task.title.enable',
    'ENABLE_SEARCH_QUERY_GENERATION': 'task.query.search.enable',
    'ENABLE_RETRIEVAL_QUERY_GENERATION': 'task.query.retrieval.enable',
    'QUERY_GENERATION_PROMPT_TEMPLATE': 'task.query.prompt_template',
    'TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE': 'task.tools.prompt_template',
    'ENABLE_VOICE_MODE_PROMPT': 'task.voice.prompt.enable',
    'VOICE_MODE_PROMPT_TEMPLATE': 'task.voice.prompt_template',
}


async def get_config_values(key_map: dict[str, str]) -> dict:
    values = await Config.get_many(*key_map.values())
    return {field: values[storage_key] for field, storage_key in key_map.items() if storage_key in values}


# Auxiliary purposes that can carry their own model instead of sharing one default.
# Each entry maps the purpose to its (local model key, external model key, human label).
AUXILIARY_MODEL_PURPOSES = {
    'title': ('task.model.title.default', 'task.model.title.external', 'title'),
    'tags': ('task.model.tags.default', 'task.model.tags.external', 'tag'),
    'followup': ('task.model.followup.default', 'task.model.followup.external', 'follow-up'),
    'query': ('task.model.query.default', 'task.model.query.external', 'search query'),
}


def config_updates(data: dict, key_map: dict[str, str]) -> dict:
    # A missing value means "not sent": keep whatever is stored instead of clearing it.
    return {key_map[field]: value for field, value in data.items() if field in key_map and value is not None}


def apply_task_model_params(payload: dict, models: dict, task_model_id: str, params: dict | None = None) -> dict:
    model = models.get(payload.get('model')) or models.get(task_model_id)
    if not model or (not params and not payload.get('params')):
        return payload
    return apply_params_to_form_data(payload, model, params or None)


async def get_task_model_generation_config(
    default_model_id: str, models, purpose: str | None = None
) -> tuple[str, dict]:
    keys = [
        'task.model.default',
        'task.model.external',
        'task.model.params',
    ]
    purpose_keys = AUXILIARY_MODEL_PURPOSES.get(purpose) if purpose else None
    if purpose_keys:
        keys.extend(purpose_keys[:2])

    config = await Config.get_many(*keys)
    params = config.get('task.model.params') or {}
    if not isinstance(params, dict):
        params = {}

    task_model = config.get('task.model.default')
    task_model_external = config.get('task.model.external')
    if purpose_keys:
        # A purpose-specific model overrides the shared default; an empty value keeps
        # inheriting it so upgrading an existing installation never disables a task.
        purpose_model = config.get(purpose_keys[0])
        purpose_external = config.get(purpose_keys[1])
        if purpose_model:
            task_model = purpose_model
        if purpose_external:
            task_model_external = purpose_external

    model_id = get_task_model_id(default_model_id, task_model, task_model_external, models)
    if not model_id:
        label = purpose_keys[2] if purpose_keys else 'auxiliary'
        raise HTTPException(409, f'Configure a dedicated {label} model; automatic fallback is disabled')
    return model_id, {key: value for key, value in params.items() if value is not None and value != ''}


##################################
#
# Task Endpoints
#
##################################


@router.get('/config')
async def get_task_config(request: Request, user=Depends(get_verified_user)):
    return await get_config_values(TASK_CONFIG_KEYS)


class TaskConfigForm(BaseModel):
    TASK_MODEL: Optional[str]
    TASK_MODEL_EXTERNAL: Optional[str]
    TASK_MODEL_PARAMS: dict | None = None
    TASK_MODEL_TITLE: Optional[str] = None
    TASK_MODEL_TITLE_EXTERNAL: Optional[str] = None
    TASK_MODEL_TAGS: Optional[str] = None
    TASK_MODEL_TAGS_EXTERNAL: Optional[str] = None
    TASK_MODEL_FOLLOWUP: Optional[str] = None
    TASK_MODEL_FOLLOWUP_EXTERNAL: Optional[str] = None
    TASK_MODEL_QUERY: Optional[str] = None
    TASK_MODEL_QUERY_EXTERNAL: Optional[str] = None
    ENABLE_TITLE_GENERATION: bool
    TITLE_GENERATION_PROMPT_TEMPLATE: str
    IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE: str
    ENABLE_AUTOCOMPLETE_GENERATION: bool
    AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH: int | None = None
    AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE: str
    TAGS_GENERATION_PROMPT_TEMPLATE: str
    FOLLOW_UP_GENERATION_PROMPT_TEMPLATE: str
    ENABLE_FOLLOW_UP_GENERATION: bool
    ENABLE_TAGS_GENERATION: bool
    ENABLE_SEARCH_QUERY_GENERATION: bool
    ENABLE_RETRIEVAL_QUERY_GENERATION: bool
    QUERY_GENERATION_PROMPT_TEMPLATE: str
    TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE: str
    ENABLE_VOICE_MODE_PROMPT: bool
    VOICE_MODE_PROMPT_TEMPLATE: Optional[str]


@router.post('/config/update')
async def update_task_config(request: Request, form_data: TaskConfigForm, user=Depends(get_admin_user)):
    data = form_data.model_dump()
    if data['AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH'] is None:
        data['AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH'] = AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH
    await Config.upsert(config_updates(data, TASK_CONFIG_KEYS))
    return await get_config_values(TASK_CONFIG_KEYS)


@router.post('/title/completions')
async def generate_title(request: Request, form_data: dict, user=Depends(get_verified_user)):
    if not await Config.get('task.title.enable'):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={'detail': 'Title generation is disabled'},
        )

    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if not model_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No model specified for title generation. Please ensure a model is selected for this chat.',
        )
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    try:
        task_model_id, task_model_params = await get_task_model_generation_config(model_id, models, 'title')
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise

    log.debug('generating chat title using model %s for user %s ', task_model_id, user.email)

    title_template = await Config.get('task.title.prompt_template')
    if title_template != '':
        template = title_template
    else:
        template = DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE

    content = await title_generation_template(template, form_data['messages'], user)
    task_model_params = task_model_params or {
        'max_tokens': models[task_model_id].get('info', {}).get('params', {}).get('max_tokens', 1000)
    }

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.TITLE_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error('Exception occurred', exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': 'An internal error has occurred.'},
        )


@router.post('/follow_up/completions')
async def generate_follow_ups(request: Request, form_data: dict, user=Depends(get_verified_user)):
    if not await Config.get('task.follow_up.enable'):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={'detail': 'Follow-up generation is disabled'},
        )

    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    try:
        task_model_id, task_model_params = await get_task_model_generation_config(model_id, models, 'followup')
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise

    log.debug('generating chat title using model %s for user %s ', task_model_id, user.email)

    follow_up_template = await Config.get('task.follow_up.prompt_template')
    if follow_up_template != '':
        template = follow_up_template
    else:
        template = DEFAULT_FOLLOW_UP_GENERATION_PROMPT_TEMPLATE

    content = await follow_up_generation_template(
        template, form_data['messages'], user, use_default=follow_up_template == ''
    )

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.FOLLOW_UP_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error('Exception occurred', exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': 'An internal error has occurred.'},
        )


@router.post('/tags/completions')
async def generate_chat_tags(request: Request, form_data: dict, user=Depends(get_verified_user)):
    if not await Config.get('task.tags.enable'):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={'detail': 'Tags generation is disabled'},
        )

    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    try:
        task_model_id, task_model_params = await get_task_model_generation_config(model_id, models, 'tags')
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise

    log.debug('generating chat tags using model %s for user %s ', task_model_id, user.email)

    tags_template = await Config.get('task.tags.prompt_template')
    if tags_template != '':
        template = tags_template
    else:
        template = DEFAULT_TAGS_GENERATION_PROMPT_TEMPLATE

    content = await tags_generation_template(template, form_data['messages'], user)

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.TAGS_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error(f'Error generating chat completion: {e}')
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'detail': 'An internal error has occurred.'},
        )


@router.post('/image_prompt/completions')
async def generate_image_prompt(request: Request, form_data: dict, user=Depends(get_verified_user)):
    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    task_model_id, task_model_params = await get_task_model_generation_config(model_id, models)

    log.debug('generating image prompt using model %s for user %s ', task_model_id, user.email)

    image_prompt_template = await Config.get('task.image.prompt_template')
    if image_prompt_template != '':
        template = image_prompt_template
    else:
        template = DEFAULT_IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE

    content = await image_prompt_generation_template(template, form_data['messages'], user)

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.IMAGE_PROMPT_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error('Exception occurred', exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': 'An internal error has occurred.'},
        )


@router.post('/queries/completions')
async def generate_queries(request: Request, form_data: dict, user=Depends(get_verified_user)):
    type = form_data.get('type')
    if type == 'web_search':
        if not await Config.get('task.query.search.enable'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.FEATURE_DISABLED('Search query generation'),
            )
    elif type == 'retrieval':
        if not await Config.get('task.query.retrieval.enable'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.FEATURE_DISABLED('Query generation'),
            )

    if getattr(request.state, 'cached_queries', None):
        log.info('Reusing cached queries: %s', request.state.cached_queries)
        return request.state.cached_queries

    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    task_model_id, task_model_params = await get_task_model_generation_config(model_id, models, 'query')

    log.debug('generating %s queries using model %s for user %s', type, task_model_id, user.email)

    query_template = await Config.get('task.query.prompt_template')
    if query_template.strip() != '':
        template = query_template
    else:
        template = DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE

    content = await query_generation_template(template, form_data['messages'], user)

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.QUERY_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': str(e)},
        )


@router.post('/auto/completions')
async def generate_autocompletion(request: Request, form_data: dict, user=Depends(get_verified_user)):
    if not await Config.get('task.autocomplete.enable'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.FEATURE_DISABLED('Autocompletion generation'),
        )

    type = form_data.get('type')
    prompt = form_data.get('prompt')
    messages = form_data.get('messages')

    autocomplete_input_max_length = await Config.get('task.autocomplete.input_max_length')
    if autocomplete_input_max_length > 0:
        if len(prompt) > autocomplete_input_max_length:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.INPUT_TOO_LONG(autocomplete_input_max_length),
            )

    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    task_model_id, task_model_params = await get_task_model_generation_config(model_id, models)

    log.debug('generating autocompletion using model %s for user %s', task_model_id, user.email)

    autocomplete_template = await Config.get('task.autocomplete.prompt_template')
    if autocomplete_template.strip() != '':
        template = autocomplete_template
    else:
        template = DEFAULT_AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE

    content = await autocomplete_generation_template(template, prompt, messages, type, user)

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.AUTOCOMPLETE_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, task_model_params)

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        log.error(f'Error generating chat completion: {e}')
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'detail': 'An internal error has occurred.'},
        )


@router.post('/emoji/completions')
async def generate_emoji(request: Request, form_data: dict, user=Depends(get_verified_user)):
    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']
    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    task_model_id, _ = await get_task_model_generation_config(model_id, models)

    log.debug('generating emoji using model %s for user %s ', task_model_id, user.email)

    template = DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE

    content = await emoji_generation_template(template, form_data['prompt'], user)

    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.EMOJI_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    payload = apply_task_model_params(payload, models, task_model_id, {'max_tokens': 4})

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': str(e)},
        )


@router.post('/moa/completions')
async def generate_moa_response(request: Request, form_data: dict, user=Depends(get_verified_user)):
    await load_provider_models(request, user)
    if getattr(request.state, 'direct', False) and hasattr(request.state, 'model'):
        models = {
            **dict(get_request_models(request).items()),
            request.state.model['id']: request.state.model,
        }
    else:
        models = get_request_models(request)

    model_id = form_data['model']

    if model_id not in models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    template = DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE

    content = moa_response_generation_template(
        template,
        form_data['prompt'],
        form_data['responses'],
    )

    payload = {
        'model': model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': form_data.get('stream', False),
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'chat_id': form_data.get('chat_id', None),
            'task': str(TASKS.MOA_RESPONSE_GENERATION),
            'task_body': form_data,
        },
    }

    # Process the payload through the pipeline
    try:
        payload = await process_pipeline_inlet_filter(request, payload, user, models)
    except Exception as e:
        raise e

    try:
        return await generate_chat_completion(request, form_data=payload, user=user)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={'detail': str(e)},
        )
