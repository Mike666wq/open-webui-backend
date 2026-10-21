"""One query boundary for lists, deep links, searches and writes."""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import and_, event, exists, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

viewer = ContextVar('resource_viewer', default=None)
maintenance = ContextVar('resource_maintenance', default=False)
BLOCKED = ('recycled', 'purging', 'paused', 'private')
_installed = False


@contextmanager
def including_recycled():
    token = maintenance.set(True)
    try:
        yield
    finally:
        maintenance.reset(token)


@contextmanager
def as_viewer(user_id):
    token = viewer.set(user_id)
    try:
        yield
    finally:
        viewer.reset(token)


def visible(kind, column):
    from open_webui.models.resources import ResourceEntry
    return ~exists(select(ResourceEntry.id).where(
        ResourceEntry.type == kind, ResourceEntry.resource_id == column,
        ResourceEntry.state.in_(BLOCKED),
    )).correlate_except(ResourceEntry)


def message_visible(model, user_id):
    from open_webui.models.resources import ResourceEntry
    return ~exists(select(ResourceEntry.id).where(
        ResourceEntry.type == 'channel_clear',
        ResourceEntry.resource_id == model.channel_id,
        ResourceEntry.user_id == user_id,
        ResourceEntry.state.in_(('recycled', 'cleared')),
        ResourceEntry.cutoff_at >= model.created_at,
    )).correlate_except(ResourceEntry)


def install_resource_visibility():
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, 'do_orm_execute')
    def resource_filter(state):
        if maintenance.get() or state.execution_options.get('include_recycled'):
            return
        if not (state.is_select or state.is_update or state.is_delete or state.is_insert):
            return
        if state.is_update or state.is_insert:
            from fastapi import HTTPException
            from open_webui.models.resources import ResourceEntry
            import json
            values = getattr(state.statement, '_values', None) or {}
            changed = {getattr(k, 'key', str(k)): getattr(v, 'value', None) for k, v in values.items()}
            interesting = {k: v for k, v in changed.items() if k in (
                'chat', 'data', 'meta', 'files', 'params', 'content', 'file_id', 'knowledge_id', 'chat_id', 'channel_id',
            )}
            if interesting:
                blocked = state.session.execute(select(ResourceEntry.resource_id).where(
                    ResourceEntry.state.in_(BLOCKED),
                )).scalars().all()
                encoded = json.dumps(interesting, default=str)
                if any(identity in encoded for identity in blocked):
                    raise HTTPException(409, 'Referenced resource is retained in the recycle bin')
        if state.is_delete and state.statement.table.name in (
            'chat', 'file', 'note', 'knowledge', 'shared_chat', 'channel',
        ):
            from fastapi import HTTPException
            raise HTTPException(409, 'Use Resources & Storage to recycle this content')
        from open_webui.internal.db import Base
        direct = {'chat': 'chat', 'file': 'file', 'note': 'note',
                  'knowledge': 'knowledge', 'shared_chat': 'share', 'channel': 'channel',
                  'knowledge_file': 'knowledge_file', 'knowledge_directory': 'knowledge_directory'}
        parents = {'chat_message': ('chat', 'chat_id'), 'chat_file': ('chat', 'chat_id'),
                   'knowledge_file': ('knowledge', 'knowledge_id'),
                   'knowledge_directory': ('knowledge', 'knowledge_id'),
                   'channel_file': ('channel', 'channel_id'), 'message': ('channel', 'channel_id')}
        options = []
        for mapper in Base.registry.mappers:
            cls, name = mapper.class_, mapper.local_table.name
            predicates = []
            if name in direct:
                predicates.append(visible(direct[name], cls.id))
            if name == 'shared_chat':
                predicates.append(visible('chat', cls.chat_id))
            if name in ('knowledge_file', 'knowledge_directory'):
                field = cls.directory_id if name == 'knowledge_file' else cls.parent_id
                predicates.append(or_(field.is_(None), visible('knowledge_directory', field)))
            if name in parents:
                kind, field = parents[name]
                predicates.append(visible(kind, getattr(cls, field)))
            if name == 'message' and viewer.get():
                predicates.append(message_visible(cls, viewer.get()))
            if name == 'channel_file' and viewer.get():
                # A guessed file ID must not regain access through an old,
                # personally hidden channel attachment.
                from open_webui.models.messages import Message
                predicates.append(or_(cls.message_id.is_(None), exists(select(Message.id).where(
                    Message.id == cls.message_id, message_visible(Message, viewer.get()),
                )).correlate_except(Message)))
            if predicates:
                options.append(with_loader_criteria(cls, and_(*predicates), include_aliases=True))
        state.statement = state.statement.options(*options)

    @event.listens_for(Session, 'before_flush')
    def guard_retained_rows(session, _context, _instances):
        if maintenance.get():
            return
        from fastapi import HTTPException
        from open_webui.models.resources import ResourceEntry
        # ORM identity maps can hold a row loaded before it was recycled.
        # Reject writes from those instances and new links to retained content.
        changed = [obj for obj in session.new.union(session.dirty).union(session.deleted)
                   if getattr(obj, '__tablename__', '') not in ('resource_entry', 'resource_task', 'resource_rule')]
        if not changed:
            return
        rows = session.execute(select(ResourceEntry.type, ResourceEntry.resource_id).where(
            ResourceEntry.state.in_(BLOCKED),
        )).all()
        blocked = {(kind, identity) for kind, identity in rows}
        identities = {identity for _, identity in blocked}
        import json
        for obj in changed:
            name = obj.__tablename__
            kind = {'shared_chat': 'share'}.get(name, name)
            if obj in session.deleted and kind in ('chat', 'file', 'note', 'knowledge', 'share', 'channel'):
                raise HTTPException(409, 'Use Resources & Storage to recycle this content')
            if (kind, getattr(obj, 'id', None)) in blocked:
                raise HTTPException(409, 'Resource is retained in the recycle bin')
            for parent_kind, field in (('chat', 'chat_id'), ('file', 'file_id'),
                                       ('knowledge', 'knowledge_id'), ('channel', 'channel_id'),
                                       ('knowledge_directory', 'directory_id'), ('knowledge_directory', 'parent_id')):
                if (parent_kind, getattr(obj, field, None)) in blocked:
                    raise HTTPException(409, 'Referenced resource is retained in the recycle bin')
            # Older clients sometimes attach files using embedded JSON only.
            # Check changed JSON, not historical values that are being removed.
            for column in obj.__mapper__.columns:
                if column.key not in ('chat', 'data', 'meta', 'files', 'params', 'content'):
                    continue
                from sqlalchemy import inspect
                if not inspect(obj).attrs[column.key].history.has_changes():
                    continue
                value = json.dumps(getattr(obj, column.key), default=str)
                if any(identity in value for identity in identities):
                    raise HTTPException(409, 'Referenced resource is retained in the recycle bin')


class ResourceContextMiddleware:
    """Bound viewer/maintenance scope for HTTP and every Socket.IO invocation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = viewer.set(None)
        bypass = maintenance.set(False)
        try:
            await self.app(scope, receive, send)
        finally:
            viewer.reset(token)
            maintenance.reset(bypass)
