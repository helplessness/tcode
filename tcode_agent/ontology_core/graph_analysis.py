"""Explainable graph coverage and conservative comparison of recorded conditions."""
from collections import defaultdict

from .conditions import compare_conditions  # Backward-compatible import for graph consumers.


def graph_statistics(stores):
    """Stream active endpoints; count identified sources, never assume independence.

    Exact sets use memory proportional to distinct endpoint/source pairs. A source
    with many assertions or revisions still counts once per scope and entity.
    """
    degree, neighbors, sources = defaultdict(int), defaultdict(set), defaultdict(set)
    for db, scope, subject, obj, active in stores:
        for a, b in db.execute(f'SELECT {subject},{obj} FROM assertion WHERE {active}'):
            degree[a] += 1
            degree[b] += 1
            if a != b:
                neighbors[a].add(b)
                neighbors[b].add(a)
        if scope == 'private':
            records = db.execute(f'SELECT {subject},{obj},source_id FROM assertion WHERE {active}')
        else:
            records = db.execute(f"""SELECT a.{subject},a.{obj},
                COALESCE(NULLIF(json_extract(a.payload,'$.source_id'),''),
                         NULLIF(json_extract(e.payload,'$.card_id'),''),
                         NULLIF(json_extract(e.payload,'$.wiki_ref'),''),NULLIF(e.wiki_ref,''))
                FROM assertion a LEFT JOIN json_each(a.payload,'$.evidence_ids') j
                LEFT JOIN evidence e ON e.id=j.value WHERE a.{active}""")
        for a, b, source in records:
            if source:
                key = (scope, str(source).split('#', 1)[0])
                sources[a].add(key)
                sources[b].add(key)
    return degree, neighbors, sources
