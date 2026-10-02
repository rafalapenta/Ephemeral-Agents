"""Linear GraphQL API client for task management."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from src.orchestration.kanban import KanbanStatus, KanbanTask, AuditEntry

logger = logging.getLogger(__name__)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

class LinearClient:
    """Client for interacting with Linear GraphQL API."""

    def __init__(self, api_key: str | None = None, team_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("LINEAR_API_KEY")
        self.team_key = team_key or os.getenv("LINEAR_TEAM_ID")
        
        if not self.api_key or not self.team_key:
            logger.warning("LINEAR_API_KEY or LINEAR_TEAM_ID not set. Linear integration is disabled.")
            
        self.headers = {
            "Authorization": str(self.api_key),
            "Content-Type": "application/json",
        }
        # State caching
        self._states: dict[str, str] = {}  # Map of state name (lowercase) to state ID

    def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query against Linear."""
        if not self.api_key:
            raise ValueError("LINEAR_API_KEY is not configured.")

        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(LINEAR_GRAPHQL_URL, headers=self.headers, json=payload)
                
                # Check for GraphQL errors even if 200 OK
                if response.status_code != 200:
                    logger.error("HTTP Error %s: %s", response.status_code, response.text)
                
                response.raise_for_status()
                data = response.json()
                
                if "errors" in data:
                    logger.error("Linear API Error: %s", data["errors"])
                    raise ValueError(f"Linear API returned errors: {data['errors']}")
                    
                return data.get("data", {})
        except Exception as exc:
            logger.error("Linear API Request failed: %s", exc)
            raise

    def fetch_workflow_states(self) -> dict[str, str]:
        """Fetch all workflow states for the team to map them dynamically."""
        if self._states:
            return self._states
            
        query = """
        query($teamKey: String!) {
          workflowStates(filter: { team: { key: { eq: $teamKey } } }) {
            nodes {
              id
              name
              type
            }
          }
        }
        """
        data = self._query(query, {"teamKey": self.team_key})
        nodes = data.get("workflowStates", {}).get("nodes", [])
        if not nodes:
            logger.warning(f"Could not find workflow states for team with key: {self.team_key}")
            
        states = {}
        for node in nodes:
            states[node["name"].lower()] = node["id"]
            
        self._states = states
        return states

    def get_state_id(self, target_state_name: str) -> str | None:
        """Get the Linear State ID for a given state name (e.g. 'Todo', 'In Progress')."""
        states = self.fetch_workflow_states()
        return states.get(target_state_name.lower())

    def fetch_ready_tasks(self) -> list[KanbanTask]:
        """Fetch tasks in the 'Todo' state and map them to KanbanTask."""
        # For our purposes, 'Todo' maps to READY
        state_id = self.get_state_id("todo")
        if not state_id:
            logger.warning("Could not find 'Todo' state in Linear.")
            return []

        query = """
        query($teamKey: String!, $stateId: ID!) {
          issues(filter: { team: { key: { eq: $teamKey } }, state: { id: { eq: $stateId } } }) {
            nodes {
              id
              identifier
              title
              description
              priority
            }
          }
        }
        """
        data = self._query(query, {"teamKey": self.team_key, "stateId": state_id})
        nodes = data.get("issues", {}).get("nodes", [])
        if not nodes:
            return []

        tasks = []
        for issue in nodes:
            # Map priority (0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low)
            priority_map = {1: "high", 2: "high", 3: "medium", 4: "low"}
            linear_prio = issue.get("priority", 0)
            priority = priority_map.get(linear_prio, "medium")

            task = KanbanTask(
                task_id=issue["id"],
                title=f"[{issue['identifier']}] {issue['title']}",
                body=issue.get("description") or "",
                status=KanbanStatus.READY,
                priority=priority,
            )
            tasks.append(task)
            
        return tasks

    def update_issue_state(self, issue_id: str, new_state_name: str) -> bool:
        """Update an issue's state (e.g. to 'In Progress' or 'Done')."""
        state_id = self.get_state_id(new_state_name)
        if not state_id:
            logger.error("Cannot update issue, state '%s' not found.", new_state_name)
            return False

        mutation = """
        mutation($issueId: String!, $stateId: String!) {
          issueUpdate(
            id: $issueId,
            input: { stateId: $stateId }
          ) {
            success
            issue {
              id
              state { name }
            }
          }
        }
        """
        try:
            data = self._query(mutation, {"issueId": issue_id, "stateId": state_id})
            return data.get("issueUpdate", {}).get("success", False)
        except Exception:
            return False

    def add_comment(self, issue_id: str, body: str) -> bool:
        """Add a comment to a Linear issue."""
        mutation = """
        mutation($issueId: String!, $body: String!) {
          commentCreate(
            input: { issueId: $issueId, body: $body }
          ) {
            success
          }
        }
        """
        try:
            data = self._query(mutation, {"issueId": issue_id, "body": body})
            return data.get("commentCreate", {}).get("success", False)
        except Exception:
            return False

# Global client singleton (lazy init)
_linear_client: LinearClient | None = None

def get_linear_client() -> LinearClient | None:
    """Get or create the Linear client, returns None if not configured."""
    global _linear_client
    if _linear_client is None:
        client = LinearClient()
        if client.api_key and client.team_key:
            _linear_client = client
    return _linear_client
