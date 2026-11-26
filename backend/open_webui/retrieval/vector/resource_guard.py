"""Apply the same recycle boundary to synchronous and asynchronous RAG calls."""

from fastapi import HTTPException
from open_webui.retrieval.vector.main import VectorDBBase
from sqlalchemy import select

# Recycle entries carry model primary keys, while vector rows carry file /
# knowledge identifiers.  Resolve every blocked entry to the identifiers that
# can appear in vector metadata in one pass, so search/get/query/hybrid_search
# share the same boundary without per-result database lookups.
_METADATA_ID_KEYS = ('file_id', 'knowledge_id', 'knowledge_base_id', 'directory_id', 'id')


def _directory_identities(db, roots):
    """Return a directory subtree's ids and its knowledge-file ids in one batch."""
    from open_webui.models.knowledge import KnowledgeDirectory, KnowledgeFile
    blocked = set(roots)
    frontier = set(roots)
    visited = set(roots)
    while frontier:
        children = set(db.scalars(select(KnowledgeDirectory.id).where(
            KnowledgeDirectory.parent_id.in_(frontier),
        )).all()) - visited
        if not children:
            break
        visited.update(children)
        blocked.update(children)
        frontier = children
    blocked.update(db.scalars(select(KnowledgeFile.file_id).where(
        KnowledgeFile.directory_id.in_(visited),
    )).all())
    return blocked


def blocked_ids(collection_name=None):
    """Return identifiers that must be filtered out of vector results.

    The mapping is keyed by what vector metadata actually stores, not by the
    resource_entry.resource_id column:

    * file -> the file id used by ``file-<id>`` collections and ``file_id``
    * knowledge -> the knowledge base id (whole collection stays blocked)
    * knowledge_file -> the associated File.id in the owning KB collection
    * knowledge_directory -> the directory id plus every descendant directory
      and file id, scoped to the owning KB

    ``collection_name`` scopes link/directory blocks to that knowledge base:
    a file recycled in one KB may still be linked and searchable in another.
    Pass ``None`` (or call from a test) to get the union across all bases.

    Everything is read in a small fixed number of queries: one for all blocked
    entries, one batched query for links and one batched query per level of the
    directory tree.  No query is issued per search result.
    """
    from open_webui.internal.db import SessionLocal
    from open_webui.models.knowledge import KnowledgeDirectory, KnowledgeFile
    from open_webui.models.resources import ResourceEntry
    from open_webui.utils.resource_visibility import BLOCKED, including_recycled, maintenance
    if maintenance.get():
        return set()
    with SessionLocal() as db, including_recycled():
        entries = (db.execute(select(
            ResourceEntry.type, ResourceEntry.resource_id, ResourceEntry.data,
        ).where(ResourceEntry.state.in_(BLOCKED)))).all()
        by_type = {}
        for kind, resource_id, _data in entries:
            by_type.setdefault(kind, set()).add(resource_id)

        # Files and knowledge bases are global: their own collections are
        # blocked regardless of which collection is being searched.
        blocked = set(by_type.get('file', ())) | set(by_type.get('knowledge', ()))

        link_ids = by_type.get('knowledge_file', ())
        if link_ids:
            query = select(KnowledgeFile).where(KnowledgeFile.id.in_(link_ids))
            if collection_name is not None:
                query = query.where(KnowledgeFile.knowledge_id == collection_name)
            for link in db.scalars(query).all():
                # A knowledge_file entry's resource_id is the link row id;
                # vectors use the file id (and sometimes the link id itself).
                blocked.add(link.id)
                blocked.add(link.file_id)

        directory_ids = by_type.get('knowledge_directory', ())
        if directory_ids:
            dir_query = select(KnowledgeDirectory.id).where(KnowledgeDirectory.id.in_(directory_ids))
            if collection_name is not None:
                dir_query = dir_query.where(KnowledgeDirectory.knowledge_id == collection_name)
            roots = set(db.scalars(dir_query).all())
            if roots:
                blocked.update(_directory_identities(db, roots))

        return blocked


def filter_result(result, blocked):
    if not result or not blocked or not result.ids or not any(result.ids):
        return result
    data = result.model_dump()
    for batch, identities in enumerate(data['ids']):
        batches = data.get('metadatas') or []
        metadata = batches[batch] if batch < len(batches) else []
        keep = []
        for i, identity in enumerate(identities):
            meta = metadata[i] if i < len(metadata) and isinstance(metadata[i], dict) else {}
            if identity not in blocked and not any(str(meta.get(k, '')) in blocked for k in _METADATA_ID_KEYS):
                keep.append(i)
        for field in ('ids', 'documents', 'metadatas', 'distances'):
            if data.get(field) is not None and batch < len(data[field]):
                values = data[field][batch]
                data[field][batch] = [values[i] for i in keep if i < len(values)]
    return type(result)(**data)


def blocked_links(collection_name):
    """Compatibility helper: links recycled in the requested knowledge base."""
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
        blocked = blocked_ids(collection_name)
        if collection_name.removeprefix('file-') in blocked:
            raise HTTPException(404, 'Resource is in the recycle bin')
        # A recycled knowledge collection keeps the original whole-collection
        # interception.  Link/directory filtering is now part of blocked_ids,
        # so no second query is needed here.
        return blocked

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
        result = self.wrapped.hybrid_search(
            collection_name, query, vectors, filter, limit, hybrid_bm25_weight,
        )
        return filter_result(result, blocked)

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
