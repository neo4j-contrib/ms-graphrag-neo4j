import os
from typing import Any, Dict, List, Optional, Type
from neo4j import Driver
from openai import AsyncOpenAI
import asyncio


from tqdm.asyncio import tqdm, tqdm_asyncio


from ms_graphrag_neo4j.cypher_queries import *
from ms_graphrag_neo4j.utils import *
from ms_graphrag_neo4j.prompts import *


class MsGraphRAG:
    """
    MsGraphRAG: Microsoft GraphRAG Implementation for Neo4j

    A class for implementing the Microsoft GraphRAG approach with Neo4j graph database.
    GraphRAG enhances retrieval-augmented generation by leveraging graph structures
    to provide context-aware information for LLM responses.

    This implementation features:
    - Entity and relationship extraction from unstructured text
    - Node and relationship summarization for improved retrieval
    - Community detection and summarization for concept clustering
    - Integration with OpenAI models for generation

    The class connects to Neo4j for graph storage and uses OpenAI for content generation
    and extraction, providing a seamless way to build knowledge graphs from text
    and perform graph-based retrieval.

    Requirements:
    - Neo4j database with APOC and GDS plugins installed
    - OpenAI API key for LLM interactions

    Example:
    ```
    from ms_graphrag_neo4j import MsGraphRAG
    import os

    os.environ["OPENAI_API_KEY"]= "sk-proj-"
    os.environ["NEO4J_URI"]="bolt://localhost:7687"
    os.environ["NEO4J_USERNAME"]="neo4j"
    os.environ["NEO4J_PASSWORD"]="password"

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]))
    ms_graph = MsGraphRAG(driver=driver, model='gpt-4o')

    example_texts = ["Tomaz works for Neo4j", "Tomaz lives in Grosuplje", "Tomaz went to school in Grosuplje"]
    allowed_entities = ["Person", "Organization", "Location"]

    await ms_graph.extract_nodes_and_rels(example_texts, allowed_entities)

    await ms_graph.summarize_nodes_and_rels()

    await ms_graph.summarize_communities()
    ```

    References:
    - Microsoft GraphRAG: https://github.com/microsoft/graphrag
    """

    def __init__(
        self,
        driver: Driver,
        model: str = "gpt-4o",
        database: str = "neo4j",
        max_workers: int = 10,
        create_constraints: bool = True,
    ) -> None:
        """
        Initialize MsGraphRAG with Neo4j driver and LLM.

        Args:
            driver (Driver): Neo4j driver instance
            model (str, optional): The language model to use. Defaults to "gpt-4o".
            database (str, optional): Neo4j database name. Defaults to "neo4j".
            max_workers (int, optional): Maximum number of concurrent workers. Defaults to 10.
            create_constraints (bool, optional): Whether to create database constraints. Defaults to True.
        """
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError(
                "You need to define the `OPENAI_API_KEY` environment variable"
            )

        self._driver = driver
        self.model = model
        self.max_workers = max_workers
        self._database = database
        self._openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        # Test for APOC
        try:
            self.query("CALL apoc.help('test')")
        except:
            raise ValueError("You need to install and allow APOC functions")
        # Test for GDS
        try:
            self.query("CALL gds.list('test')")
        except:
            raise ValueError("You need to install and allow GDS functions")
        if create_constraints:
            self.query(
                "CREATE CONSTRAINT IF NOT EXISTS FOR (e:__Chunk__) REQUIRE e.id IS UNIQUE;"
            )
            self.query(
                "CREATE CONSTRAINT IF NOT EXISTS FOR (e:__Entity__) REQUIRE e.name IS UNIQUE;"
            )
            self.query(
                "CREATE CONSTRAINT IF NOT EXISTS FOR (e:__Community__) REQUIRE e.id IS UNIQUE;"
            )

    async def extract_nodes_and_rels(
        self, input_texts: list, allowed_entities: list
    ) -> str:
        """
        Extract nodes and relationships from input texts using LLM and store them in Neo4j.

        Args:
            input_texts (list): List of text documents to process and extract entities from
            allowed_entities (list): List of entity types to extract from the texts

        Returns:
            str: Success message with count of extracted relationships

        Notes:
            - Uses parallel processing with tqdm progress tracking
            - Extracted entities and relationships are stored directly in Neo4j
            - Each text document is processed independently by the LLM
        """

        async def process_text(input_text):
            prompt = GRAPH_EXTRACTION_PROMPT.format(
                entity_types=allowed_entities,
                input_text=input_text,
                tuple_delimiter=";",
                record_delimiter="|",
                completion_delimiter="\n\n",
            )
            messages = [
                {"role": "user", "content": prompt},
            ]
            # Make the LLM call
            output = await self.achat(messages, model=self.model)
            # Construct JSON from output
            return parse_extraction_output(output.content)

        # Create tasks for all input texts
        tasks = [process_text(text) for text in input_texts]

        # Process tasks with tqdm progress bar
        # Use semaphore to limit concurrent tasks if max_workers is specified
        if self.max_workers:
            semaphore = asyncio.Semaphore(self.max_workers)

            async def process_with_semaphore(task):
                async with semaphore:
                    return await task

            results = []
            for task in tqdm.as_completed(
                [process_with_semaphore(task) for task in tasks],
                total=len(tasks),
                desc="Extracting nodes & relationships",
            ):
                results.append(await task)
        else:
            results = []
            for task in tqdm.as_completed(
                tasks, total=len(tasks), desc="Extracting nodes & relationships"
            ):
                results.append(await task)

        total_relationships = 0
        # Import nodes and relationships
        for text, output in zip(input_texts, results):
            nodes, relationships = output
            total_relationships += len(relationships)
            # Import nodes
            self.query(
                import_nodes_query,
                params={"text": text, "chunk_id": get_hash(text), "data": nodes},
            )
            # Import relationships
            self.query(import_relationships_query, params={"data": relationships})

        return f"Successfuly extracted and imported {total_relationships} relationships"

    async def summarize_nodes_and_rels(self) -> str:
        """
        Generate summaries for all nodes and relationships in the graph.

        Returns:
            str: Success message indicating completion of summarization

        Notes:
            - Retrieves candidate nodes and relationships from Neo4j
            - Uses LLM to generate concise summaries for each entity and relationship
            - Stores summarized properties in the graph
        """
        # Summarize nodes
        nodes = self.query(candidate_nodes_summarization)

        async def process_node(node):
            messages = [
                {
                    "role": "user",
                    "content": SUMMARIZE_PROMPT.format(
                        entity_name=node["entity_name"],
                        description_list=node["description_list"],
                    ),
                },
            ]
            summary = await self.achat(messages, model=self.model)
            return {"entity": node["entity_name"], "summary": summary.content}

        # Create a progress bar for node processing with max_workers limit
        if self.max_workers:
            semaphore = asyncio.Semaphore(self.max_workers)

            async def process_with_semaphore(node):
                async with semaphore:
                    return await process_node(node)

            summaries = await tqdm_asyncio.gather(
                *[process_with_semaphore(node) for node in nodes],
                desc="Summarizing nodes",
            )
        else:
            summaries = await tqdm_asyncio.gather(
                *[process_node(node) for node in nodes], desc="Summarizing nodes"
            )

        # Summarize relationships
        rels = self.query(candidate_rels_summarization)

        async def process_rel(rel):
            entity_name = f"{rel['source']} relationship to {rel['target']}"
            messages = [
                {
                    "role": "user",
                    "content": SUMMARIZE_PROMPT.format(
                        entity_name=entity_name,
                        description_list=rel["description_list"],
                    ),
                },
            ]
            summary = await self.achat(messages, model=self.model)
            return {
                "source": rel["source"],
                "target": rel["target"],
                "summary": summary.content,
            }

        # Create a progress bar for relationship processing with max_workers limit
        if self.max_workers:
            semaphore = asyncio.Semaphore(self.max_workers)

            async def process_rel_with_semaphore(rel):
                async with semaphore:
                    return await process_rel(rel)

            rel_summaries = await tqdm_asyncio.gather(
                *[process_rel_with_semaphore(rel) for rel in rels],
                desc="Summarizing relationships",
            )
        else:
            rel_summaries = await tqdm_asyncio.gather(
                *[process_rel(rel) for rel in rels], desc="Summarizing relationships"
            )

        # Import nodes
        self.query(import_entity_summary, params={"data": summaries})
        self.query(import_entity_summary_single)

        # Import relationships
        self.query(import_rel_summary, params={"data": rel_summaries})
        self.query(import_rel_summary_single)

        return "Successfuly summarized nodes and relationships"

    async def summarize_communities(self, summarize_all_levels: bool = False) -> str:
        """
        Detect and summarize communities within the graph using the Leiden algorithm.

        Args:
            summarize_all_levels (bool, optional): Whether to summarize all community levels
                or just the final level. Defaults to False.

        Returns:
            str: Success message with count of generated community summaries

        Notes:
            - Uses Neo4j GDS library to run Leiden community detection algorithm
            - Generates hierarchical community structures in the graph
            - Uses LLM to create descriptive summaries of each community
            - The community summaries include key entities, relationships, and themes
        """
        # Calculate communities
        self.query(drop_gds_graph_query)
        self.query(create_gds_graph_query)
        community_summary = self.query(leiden_query)
        community_levels = community_summary[0]["ranLevels"]
        print(
            f"Leiden algorithm identified {community_levels} community levels "
            f"with {community_summary[0]['communityCount']} communities on the last level."
        )
        self.query(community_hierarchy_query)

        # Community summarization
        if summarize_all_levels:
            levels = list(range(community_levels))
        else:
            levels = [community_levels - 1]
        communities = self.query(community_info_query, params={"levels": levels})

        # Define async function for processing a single community
        async def process_community(community):
            input_text = f"""Entities:
                    {community['nodes']}

                    Relationships:
                    {community['rels']}"""

            messages = [
                {
                    "role": "user",
                    "content": COMMUNITY_REPORT_PROMPT.format(input_text=input_text),
                },
            ]
            summary = await self.achat(messages, model=self.model)
            return {
                "community": extract_json(summary.content),
                "communityId": community["communityId"],
            }

        # Process all communities concurrently with tqdm progress bar and max_workers limit
        if self.max_workers:
            semaphore = asyncio.Semaphore(self.max_workers)

            async def process_community_with_semaphore(community):
                async with semaphore:
                    return await process_community(community)

            community_summary = await tqdm_asyncio.gather(
                *(
                    process_community_with_semaphore(community)
                    for community in communities
                ),
                desc="Summarizing communities",
                total=len(communities),
            )
        else:
            community_summary = await tqdm_asyncio.gather(
                *(process_community(community) for community in communities),
                desc="Summarizing communities",
                total=len(communities),
            )

        self.query(import_community_summary, params={"data": community_summary})
        return f"Generated {len(community_summary)} community summaries"

    def _check_driver_state(self) -> None:
        """
        Check if the Neo4j driver is still available.

        Raises:
            RuntimeError: If the Neo4j driver has been closed.
        """
        if not hasattr(self, "_driver"):
            raise RuntimeError(
                "This MsGraphRAG instance has been closed, and cannot be used anymore."
            )

    def query(
        self,
        query: str,
        params: dict = {},
        session_params: dict = {},
    ) -> List[Dict[str, Any]]:
        """Query Neo4j database.

        Args:
            query (str): The Cypher query to execute.
            params (dict): The parameters to pass to the query.
            session_params (dict): Parameters to pass to the session used for executing
                the query.

        Returns:
            List[Dict[str, Any]]: The list of dictionaries containing the query results.

        Raises:
            RuntimeError: If the connection has been closed.
        """
        self._check_driver_state()
        from neo4j import Query
        from neo4j.exceptions import Neo4jError

        if not session_params:
            try:
                data, _, _ = self._driver.execute_query(
                    Query(text=query),
                    database_=self._database,
                    parameters_=params,
                )
                return [r.data() for r in data]
            except Neo4jError as e:
                if not (
                    (
                        (  # isCallInTransactionError
                            e.code == "Neo.DatabaseError.Statement.ExecutionFailed"
                            or e.code
                            == "Neo.DatabaseError.Transaction.TransactionStartFailed"
                        )
                        and e.message is not None
                        and "in an implicit transaction" in e.message
                    )
                    or (  # isPeriodicCommitError
                        e.code == "Neo.ClientError.Statement.SemanticError"
                        and e.message is not None
                        and (
                            "in an open transaction is not possible" in e.message
                            or "tried to execute in an explicit transaction"
                            in e.message
                        )
                    )
                ):
                    raise
        # fallback to allow implicit transactions
        session_params.setdefault("database", self._database)
        with self._driver.session(**session_params) as session:
            result = session.run(Query(text=query, timeout=self.timeout), params)
            return [r.data() for r in result]

    async def achat(self, messages, model="gpt-4o", config={}):
        response = await self._openai_client.chat.completions.create(
            model=model,
            messages=messages,
            **config,
        )
        return response.choices[0].message

    def close(self) -> None:
        """
        Explicitly close the Neo4j driver connection.

        Delegates connection management to the Neo4j driver.
        """
        if hasattr(self, "_driver"):
            self._driver.close()
            # Remove the driver attribute to indicate closure
            delattr(self, "_driver")

    def __enter__(self) -> "MsGraphRAG":
        """
        Enter the runtime context for the Neo4j graph connection.

        Enables use of the graph connection with the 'with' statement.
        This method allows for automatic resource management and ensures
        that the connection is properly handled.

        Returns:
            MsGraphRAG: The current graph connection instance

        Example:
            with MsGraphRAG(...) as graph:
                graph.query(...)  # Connection automatically managed
        """
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """
        Exit the runtime context for the Neo4j graph connection.

        This method is automatically called when exiting a 'with' statement.
        It ensures that the database connection is closed, regardless of
        whether an exception occurred during the context's execution.

        Args:
            exc_type: The type of exception that caused the context to exit
                      (None if no exception occurred)
            exc_val: The exception instance that caused the context to exit
                     (None if no exception occurred)
            exc_tb: The traceback for the exception (None if no exception occurred)

        Note:
            Any exception is re-raised after the connection is closed.
        """
        self.close()

    def __del__(self) -> None:
        """
        Destructor for the Neo4j graph connection.

        This method is called during garbage collection to ensure that
        database resources are released if not explicitly closed.

        Caution:
            - Do not rely on this method for deterministic resource cleanup
            - Always prefer explicit .close() or context manager

        Best practices:
            1. Use context manager:
               with MsGraphRAG(...) as graph:
                   ...
            2. Explicitly close:
               graph = MsGraphRAG(...)
               try:
                   ...
               finally:
                   graph.close()
        """
        try:
            self.close()
        except Exception:
            # Suppress any exceptions during garbage collection
            pass
