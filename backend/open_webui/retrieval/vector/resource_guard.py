"""Apply the same recycle boundary to synchronous and asynchronous RAG calls."""

from fastapi import HTTPException
from sqlalchemy import select

from open_webui.retrieval.vector.main import VectorDBBase


def blocked_ids():
    from open_webui.internal.db import SessionLocal
    from open_webui.models.resources import ResourceEntry
    from open_webui.utils.resource_visibility import BLOCKED, maintenance
    if maintenance.get():
        return set()
    with SessionLocal() as db:
        return set(db.scalars(select(ResourceEntry.resource_id).where(
            ResourceEntry.type.in_(('file', 'knowledge')),
            ResourceEntry.state.in_(BLOCKED),
        )).all())


def filter_result(result, blocked):
    if not result or not blocked or not result.ids:
        return result
    data = result.model_dump()
    for batch, identities in enumerate(data['ids']):
        batches = data.get('metadatas') or []
        metadata = batches[batch] if batch < len(batches) else []
        keep = []
        for i, identity in enumerate(identities):
            meta = metadata[i] if i < len(metadata) and isinstance(metadata[i], dict) else {}
            if identity not in blocked and not any(str(meta.get(k, '')) in blocked for k in ('file_id', 'knowledge_id', 'knowledge_base_id', 'id')):
                keep.append(i)
        for field in ('ids', 'documents', 'metadatas', 'distances'):
            if data.get(field) is not None and batch < len(data[field]):
                values = data[field][batch]
                data[field][batch] = [values[i] for i in keep if i < len(values)]
    return type(result)(**data)


def blocked_links(collection_name):
    from open_webui.internal.db import SessionLocal
    from open_webui.models.knowledge import KnowledgeFile
    from open_webui.models.resources import ResourceEntry
    from open_webui.utils.resource_visibility import BLOCKED, including_recycled, maintenance
    if maintenance.get():
        return set()
    with SessionLocal() as db, including_recycled():
        return set(db.scalars(select(KnowledgeFile.file_id).join(
            ResourceEntry, ResourceEntry.resource_id == KnowledgeFile.id,
        ).where(KnowledgeFile.knowledge_id == collection_name,
                ResourceEntry.type == 'knowledge_file', ResourceEntry.state.in_(BLOCKED))).all())


class ResourceVectorGuard(VectorDBBase):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def _require_visible(self, collection_name):
        blocked = blocked_ids()
        if collection_name.removeprefix('file-') in blocked:
            raise HTTPException(404, 'Resource is in the recycle bin')
        return blocked | blocked_links(collection_name)

    def has_collection(self, collection_name):
        self._require_visible(collection_name)
        return self.wrapped.has_collection(collection_name)

    def delete_collection(self, collection_name):
        self._require_visible(collection_name)
        return self.wrapped.delete_collection(collection_name)

    def insert(self, collection_name, items):
        self._require_visible(collection_name)
        return self.wrapped.insert(collection_name, items)

    def upsert(self, collection_name, items):
        self._require_visible(collection_name)
        return self.wrapped.upsert(collection_name, items)

    def search(self, collection_name, vectors, filter=None, limit=10):
        blocked = self._require_visible(collection_name)
        return filter_result(self.wrapped.search(collection_name, vectors, filter, limit), blocked)

    def hybrid_search(self, collection_name, query, vectors, filter=None, limit=10, hybrid_bm25_weight=0.5):
        blocked = self._require_visible(collection_name)
        return filter_result(self.wrapped.hybrid_search(collection_name, query, vectors, filter, limit, hybrid_bm25_weight), blocked)

    def query(self, collection_name, filter, limit=None):
        blocked = self._require_visible(collection_name)
        return filter_result(self.wrapped.query(collection_name, filter, limit), blocked)

    def get(self, collection_name):
        blocked = self._require_visible(collection_name)
        return filter_result(self.wrapped.get(collection_name), blocked)

    def delete(self, collection_name, ids=None, filter=None):
        self._require_visible(collection_name)
        return self.wrapped.delete(collection_name, ids, filter)

    def reset(self):
        from open_webui.utils.resource_visibility import maintenance
        if not maintenance.get():
            raise HTTPException(409, 'Use resource cleanup tasks; vector reset bypasses retention')
        return self.wrapped.reset()


def guard_vector(client):
    return client if isinstance(client, ResourceVectorGuard) else ResourceVectorGuard(client)
