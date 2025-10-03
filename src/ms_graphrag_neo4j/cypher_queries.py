import_nodes_query = """
CREATE (c:__Chunk__ {id: $chunk_id})
SET c.text = $text
WITH c
UNWIND $data AS row
MERGE (n:__Entity__ {name: row.entity_name})
SET n:$(row.entity_type),
    n.description = coalesce(n.description, []) + [row.entity_description]
MERGE (n)<-[:MENTIONS]-(c)
"""

import_relationships_query = """
UNWIND $data AS row
MERGE (s:__Entity__ {name: row.source_entity})
MERGE (t:__Entity__ {name: row.target_entity})
CREATE (s)-[r:RELATIONSHIP {description: row.relationship_description, strength: row.relationship_strength}]->(t)
"""

import_community_query = """
UNWIND $data AS row
MERGE (c:__Community__ {communityId: row.communityId})
SET c.title = row.community.title,
    c.summary = row.community.summary,
    c.rating = row.community.rating,
    c.rating_explanation = row.community.rating_explanation
WITH c, row
UNWIND row.nodes AS node
MERGE (n:__Entity__ {name: node})
MERGE (n)-[:IN_COMMUNITY]->(c)
"""

candidate_nodes_summarization = """
MATCH (e:__Entity__) WHERE size(e.description) > 1 
RETURN e.name AS entity_name, e.description AS description_list
"""

candidate_rels_summarization = """
MATCH (s:__Entity__)-[r:RELATIONSHIP]-(t:__Entity__)
WHERE elementId(s) < elementId(t)
WITH s.name AS source, t.name AS target, 
        collect(r.description) AS description_list,
        count(*) AS count
WHERE count > 1
RETURN source, target, description_list
"""

import_entity_summary = """
UNWIND $data AS row
MATCH (e:__Entity__ {name: row.entity})
SET e.summary = row.summary
"""

import_entity_summary = """
UNWIND $data AS row
MATCH (e:__Entity__ {name: row.entity})
SET e.summary = row.summary
"""

import_entity_summary_single = """
MATCH (e:__Entity__)
WHERE size(e.description) = 1
SET e.summary = e.description[0]
"""

import_rel_summary = """
UNWIND $data AS row
MATCH (s:__Entity__ {name: row.source}), (t:__Entity__ {name: row.target})
MERGE (s)-[r:SUMMARIZED_RELATIONSHIP]-(t)
SET r.summary = row.summary
"""

import_rel_summary_single = """
MATCH (s:__Entity__)-[e:RELATIONSHIP]-(t:__Entity__)
WHERE NOT (s)-[:SUMMARIZED_RELATIONSHIP]-(t)
MERGE (s)-[r:SUMMARIZED_RELATIONSHIP]-(t)
SET r.summary = e.description
"""

drop_gds_graph_query = "CALL gds.graph.drop('entity', False) YIELD graphName"

create_gds_graph_query = """
MATCH (source:__Entity__)-[r:RELATIONSHIP]->(target:__Entity__)
WITH gds.graph.project('entity', source, target, {}, {undirectedRelationshipTypes: ['*']}) AS g
RETURN
    g.graphName AS graph, g.nodeCount AS nodes, g.relationshipCount AS rels
"""

leiden_query = """
CALL gds.leiden.write("entity", {writeProperty:"communities", includeIntermediateCommunities: True})
"""

community_hierarchy_query = """
MATCH (e:`__Entity__`)
WHERE e.communities IS NOT NULL
UNWIND range(0, size(e.communities) - 1 , 1) AS index
CALL (e, index) {
  WITH e, index
  WHERE index = 0
  MERGE (c:`__Community__` {id: toString(index) + '-' + toString(e.communities[index])})
  ON CREATE SET c.level = index
  MERGE (e)-[:IN_COMMUNITY]->(c)
  RETURN count(*) AS count_0
}
CALL (e, index) {
  WITH e, index
  WHERE index > 0
  MERGE (current:`__Community__` {id: toString(index) + '-' + toString(e.communities[index])})
  ON CREATE SET current.level = index
  MERGE (previous:`__Community__` {id: toString(index - 1) + '-' + toString(e.communities[index - 1])})
  ON CREATE SET previous.level = index - 1
  MERGE (previous)-[:IN_COMMUNITY]->(current)
  RETURN count(*) AS count_1
}
RETURN count(*)
"""

community_info_query = """
MATCH (c:`__Community__`)<-[:IN_COMMUNITY*]-(e:__Entity__)
WHERE c.level IN $levels
WITH c, collect(e ) AS nodes
WHERE size(nodes) > 1
CALL apoc.path.subgraphAll(nodes[0], {
	whitelistNodes:nodes,
    relationshipFilter: "SUMMARIZED_RELATIONSHIP"
})
YIELD relationships
RETURN c.id AS communityId,
       [n in nodes | {id: n.name, description: n.summary, type: [el in labels(n) WHERE el <> '__Entity__'][0]}] AS nodes,
       [r in relationships | {start: startNode(r).name, type: type(r), end: endNode(r).name, description: r.summary}] AS rels
"""

import_community_summary = """
UNWIND $data AS row
MERGE (c:__Community__ {id: row.communityId})
SET c.title = row.community.title,
    c.summary = row.community.summary,
    c.rating = row.community.rating,
    c.rating_explanation = row.community.rating_explanation
"""
