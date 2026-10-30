import logging
from typing import Any

from open_webui.models.access_grants import AccessGrants
from open_webui.models.channels import Channels
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.models.folders import FolderModel
from open_webui.models.groups import Groups
from open_webui.models.knowledge import Knowledges
from open_webui.models.knowledge import KnowledgeFile
from open_webui.models.models import Models
from open_webui.models.notes import Notes
from open_webui.models.resources import ResourceEntry
from open_webui.models.users import UserModel, Users
from open_webui.utils.knowledge_import_lifecycle import has_managed_marker, validate_managed_file
from open_webui.utils.resource_visibility import BLOCKED
from open_webui.internal.db import get_async_db_context
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

FOLDER_FILE_TYPES = {'file', 'collection', 'note'}


async def check_knowledge_import_access(
    file: Any, user: UserModel, access_type: str, db: AsyncSession | None = None
) -> bool | None:
    """Return the final v2 verdict, or None for ordinary files.

    v2 imports belong to their source knowledge base, not to the uploader.
    Unknown or malformed managed metadata fails closed.
    """
    if file is None:
        return False
    if not has_managed_marker(file):
        return None
    valid = validate_managed_file(file)
    if not valid:
        return False
    if valid.get('state') != 'completed':
        return False
    knowledge = await Knowledges.get_knowledge_by_id(valid['knowledge_id'], db=db)
    if not knowledge:
        return False
    async with get_async_db_context(db) as session:
        association = (await session.execute(
            select(KnowledgeFile.id).where(
                KnowledgeFile.knowledge_id == valid['knowledge_id'],
                KnowledgeFile.file_id == file.id,
            )
        )).scalar_one_or_none()
        hidden = (await session.execute(
            select(ResourceEntry.id).where(
                ResourceEntry.state.in_(BLOCKED),
                (
                    ((ResourceEntry.type == 'knowledge') & (ResourceEntry.resource_id == valid['knowledge_id']))
                    | ((ResourceEntry.type == 'file') & (ResourceEntry.resource_id == file.id))
                ),
            ).limit(1)
        )).scalar_one_or_none()
    if not association:
        return False
    if hidden:
        return False
    if user.role == 'admin':
        return True
    if knowledge.user_id == user.id:
        return True
    user_group_ids = {g.id for g in await Groups.get_groups_by_member_id(user.id, db=db, include_inherited=True)}
    allowed = await AccessGrants.has_access(
        user_id=user.id,
        resource_type='knowledge',
        resource_id=knowledge.id,
        permission='write',
        user_group_ids=user_group_ids,
        db=db,
    )
    if allowed or access_type != 'read':
        return allowed
    return await AccessGrants.has_access(
        user_id=user.id,
        resource_type='knowledge',
        resource_id=knowledge.id,
        permission='read',
        user_group_ids=user_group_ids,
        db=db,
    )


async def has_access_to_file(
    file_id: str | None,
    access_type: str,
    user: UserModel,
    db: AsyncSession | None = None,
    user_group_ids: set[str] | None = None,
) -> bool:
    """
    Check if a user has the specified access to a file through any of:
    - Knowledge bases (ownership or access grants)
    - Shared workspace models that attach the file directly
    - Channels the user is a member of
    - Shared chats
    - Shared notes whose owner owns the attached file (read only)

    NOTE: This does NOT check direct file ownership — callers should check
    file.user_id == user.id separately before calling this.
    """
    file = await Files.get_file_by_id(file_id, db=db)
    log.debug('Checking if user has %s access to file', access_type)
    if not file:
        return False

    v2_verdict = await check_knowledge_import_access(file, user, access_type, db=db)
    if v2_verdict is not None:
        return v2_verdict

    # Direct ownership
    if file.user_id == user.id:
        return True

    # Check if the file is associated with any knowledge bases the user has access to.
    # An object (knowledge base or workspace model) confers write/delete on a file only when
    # the object's OWNER owns that file; otherwise a read-only file laundered into an object
    # the user controls would gain write/delete on it (CWE-863). Read access is unaffected.
    knowledge_bases = await Knowledges.get_knowledges_by_file_id(file_id, db=db)
    if user_group_ids is None:
        user_group_ids = {
            group.id for group in await Groups.get_groups_by_member_id(user.id, db=db, include_inherited=True)
        }
    for knowledge_base in knowledge_bases:
        if user.role == 'admin':
            # Admin access to knowledge files follows knowledge endpoints, not raw file ownership.
            if await Knowledges.check_access_by_user_id(
                knowledge_base.id, user.id, access_type, db=db, user_group_ids=user_group_ids
            ):
                return True
            continue
        if (
            knowledge_base.user_id == user.id
            or await AccessGrants.has_access(
                user_id=user.id,
                resource_type='knowledge',
                resource_id=knowledge_base.id,
                permission=access_type,
                user_group_ids=user_group_ids,
                db=db,
            )
        ) and (access_type == 'read' or knowledge_base.user_id == file.user_id):
            return True

    # Check if the file is associated with any channels the user has access to
    channels = await Channels.get_channels_by_file_id_and_user_id(file_id, user.id, db=db)
    if access_type == 'read' and channels:
        return True

    # Check if the file is associated with any chats the user has access to
    shared_chat_ids = await Chats.get_shared_chat_ids_by_file_id(file_id, db=db)
    if access_type == 'read' and shared_chat_ids:
        accessible_ids = await AccessGrants.get_accessible_resource_ids(
            user_id=user.id,
            resource_type='shared_chat',
            resource_ids=shared_chat_ids,
            permission='read',
            user_group_ids=user_group_ids,
            db=db,
        )
        if accessible_ids:
            return True
        for chat_id in shared_chat_ids:
            if await Chats.get_accessible_chat_by_id(chat_id, user, db=db):
                return True

    # Note attachment JSON is user-controlled, so only the file owner's notes can grant access.
    if access_type == 'read':
        note_ids = await Notes.get_note_ids_by_file_id(file.id, owner_id=file.user_id, db=db)
        if note_ids and await AccessGrants.get_accessible_resource_ids(
            user_id=user.id,
            resource_type='note',
            resource_ids=note_ids,
            permission='read',
            user_group_ids=user_group_ids,
            db=db,
        ):
            return True

    # Check if the file is directly attached to a shared workspace model (per the ownership
    # note above, model write is conferred only for files the model owner owns).
    model_owners = await Models.get_model_owner_ids_by_file_id(file.id, db=db, include_background=access_type == 'read')
    if access_type != 'read':
        model_owners = {model_id: owner_id for model_id, owner_id in model_owners.items() if owner_id == file.user_id}
    if user.id in model_owners.values():
        return True

    return bool(
        await AccessGrants.get_accessible_resource_ids(
            user_id=user.id,
            resource_type='model',
            resource_ids=list(model_owners),
            permission=access_type,
            user_group_ids=user_group_ids,
            db=db,
        )
    )


async def can_access_file_row(
    file: Any,
    user: UserModel,
    access_type: str,
    db: AsyncSession | None = None,
) -> bool:
    """Authoritative row-level gate for callers that already hold the File row.

    v2 imports are decided solely by their source knowledge base; ordinary
    files keep the existing owner/admin/shared semantics.
    """
    if file is None:
        return False
    v2_verdict = await check_knowledge_import_access(file, user, access_type, db=db)
    if v2_verdict is not None:
        return v2_verdict
    return bool(
        file.user_id == user.id
        or user.role == 'admin'
        or await has_access_to_file(file.id, access_type, user, db=db)
    )


async def get_accessible_folder_files(
    entries: list[dict] | None,
    user: UserModel,
    db: AsyncSession | None = None,
    user_group_ids: set[str] | None = None,
) -> list[dict]:
    """Filter folder.data['files'] entries to those the caller can read.

    Entries carry a 'type' ('file', 'collection' or 'note') and 'id'. Entries of any other
    shape are dropped because they cannot be access-checked.
    """
    if not isinstance(entries, list):
        return []
    entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get('type') in FOLDER_FILE_TYPES and entry.get('id')
    ]
    if user_group_ids is None:
        user_group_ids = {
            group.id for group in await Groups.get_groups_by_member_id(user.id, db=db, include_inherited=True)
        }

    accessible: list[dict] = []
    for entry in entries:
        entry_type = entry.get('type')
        entry_id = entry.get('id')
        if entry_type == 'file':
            if await has_access_to_file(entry_id, 'read', user, db=db, user_group_ids=user_group_ids):
                accessible.append(entry)
        elif entry_type == 'collection':
            if await Knowledges.check_access_by_user_id(
                entry_id, user.id, 'read', db=db, user_group_ids=user_group_ids
            ):
                accessible.append(entry)
        elif entry_type == 'note':
            # Owner has no self-grant (notes are private by default), so check ownership too.
            note = await Notes.get_note_by_id(entry_id, db=db)
            if note and (
                note.user_id == user.id
                or await AccessGrants.has_access(
                    user_id=user.id,
                    resource_type='note',
                    resource_id=entry_id,
                    permission='read',
                    user_group_ids=user_group_ids,
                    db=db,
                )
            ):
                accessible.append(entry)
    return accessible


async def can_read_all_folder_files(
    entries: list[dict] | None,
    user: UserModel,
    db: AsyncSession | None = None,
) -> bool:
    if entries is None:
        return True
    if not isinstance(entries, list):
        return False
    if not entries:
        return True

    return len(await get_accessible_folder_files(entries, user, db=db)) == len(entries)


async def get_owner_accessible_folder_files(folder: FolderModel, db: AsyncSession | None = None) -> list[dict]:
    """Return the folder entries its owner can still delegate."""
    files = (folder.data or {}).get('files') or []
    if not files:
        return []

    owner = await Users.get_user_by_id(folder.user_id, db=db)
    if not owner:
        return []

    return await get_accessible_folder_files(files, owner, db=db)
