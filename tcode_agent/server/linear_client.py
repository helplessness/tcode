"""Linear GraphQL client used by Symphony issue endpoints."""

import os
from typing import Any, Optional

import aiohttp
from fastapi import HTTPException


def _linear_api_key() -> Optional[str]:
    return os.environ.get("LINEAR_API_KEY")


def _linear_project_slug() -> Optional[str]:
    return os.environ.get("LINEAR_PROJECT_SLUG")


async def _linear_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    api_key = _linear_api_key()
    if not api_key:
        raise HTTPException(400, "LINEAR_API_KEY is required")

    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.linear.app/graphql", json=payload, headers=headers
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise HTTPException(resp.status, data)
            if "errors" in data and data["errors"]:
                raise HTTPException(400, data["errors"])
            return data.get("data", {})


async def _linear_project_info(slug: str) -> dict[str, str]:
    query = """
        query ProjectBySlug($slug: String!) {
          projects(filter: { slugId: { eq: $slug } }) {
                        nodes { id name slugId teams { nodes { id } } }
          }
        }
    """
    data = await _linear_graphql(query, {"slug": slug})
    nodes = (data.get("projects") or {}).get("nodes") or []
    if not nodes:
        raise HTTPException(404, f"Linear project not found: {slug}")
    project = nodes[0]
    team_nodes = (project.get("teams") or {}).get("nodes") or []
    team_id = str(((team_nodes[0] if team_nodes else {}).get("id") or "")).strip()
    if not team_id:
        raise HTTPException(500, f"Linear project has no team id: {slug}")
    return {
        "project_id": project["id"],
        "team_id": team_id,
    }


async def _linear_list_projects(search: Optional[str] = None) -> list[dict[str, Any]]:
    query = """
        query ListProjects {
          projects {
            nodes { id name slugId }
          }
        }
    """
    data = await _linear_graphql(query, {})
    nodes = (data.get("projects") or {}).get("nodes") or []

    normalized = (search or "").strip().lower()
    projects: list[dict[str, Any]] = []
    for node in nodes:
        item = {
            "id": node.get("id"),
            "name": node.get("name"),
            "slugId": node.get("slugId"),
        }
        if normalized:
            haystack = " ".join(
                [
                    str(item.get("name") or "").lower(),
                    str(item.get("slugId") or "").lower(),
                ]
            )
            if normalized not in haystack:
                continue
        projects.append(item)

    projects.sort(key=lambda p: str(p.get("name") or "").lower())
    return projects


async def _linear_create_issue(
    project_id: str, team_id: str, title: str, description: str
) -> dict[str, Any]:
    mutation = """
        mutation CreateIssue($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            issue { id identifier title url }
          }
        }
    """
    data = await _linear_graphql(
        mutation,
        {
            "input": {
                "teamId": team_id,
                "projectId": project_id,
                "title": title,
                "description": description,
            }
        },
    )
    issue = ((data.get("issueCreate") or {}).get("issue")) or {}
    if not issue:
        raise HTTPException(500, "Failed to create Linear issue")
    return issue
