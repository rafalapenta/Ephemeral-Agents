import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

class ObsidianMemory:
    """Manages reading and writing memory files to an Obsidian vault."""

    def __init__(self, vault_path: str | Path | None = None):
        if not vault_path:
            vault_path = os.environ.get("OBSIDIAN_VAULT_PATH", "./data/obsidian_vault")
            
        self.vault_path = Path(vault_path)
        self.agency_dir = self.vault_path / "AGency"
        self.shared_dir = self.agency_dir / "Shared"
        self.agents_dir = self.agency_dir / "Agents"
        
        self.ensure_structure()

    def ensure_structure(self) -> None:
        """Ensures the base directory structure exists in the vault."""
        try:
            self.shared_dir.mkdir(parents=True, exist_ok=True)
            self.agents_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create Obsidian folder structure at {self.vault_path}: {e}")

    def ensure_agent_folder(self, agent_id: str) -> Path:
        """Ensures a specific agent's folder exists and creates default templates if missing."""
        agent_dir = self.agents_dir / agent_id
        if not agent_dir.exists():
            agent_dir.mkdir(parents=True, exist_ok=True)
            
            # Create default empty files so the user sees them in Obsidian
            (agent_dir / "core_directives.md").write_text(
                f"# Core Directives for {agent_id}\n\nWrite long-term rules for this agent here.\n", 
                encoding="utf-8"
            )
            (agent_dir / "current_state.md").write_text(
                f"# Current State for {agent_id}\n\nWrite current context or project status here.\n",
                encoding="utf-8"
            )
            
        return agent_dir

    def get_agent_context(self, agent_id: str) -> str:
        """Reads the core memory files for the given agent and shared memory."""
        self.ensure_structure()
        agent_dir = self.ensure_agent_folder(agent_id)
        
        context_parts = []
        
        # 1. Read Shared Context (if it exists)
        shared_file = self.shared_dir / "shared_context.md"
        if shared_file.exists():
            content = shared_file.read_text(encoding="utf-8").strip()
            if content:
                context_parts.append(f"--- SHARED KNOWLEDGE ---\n{content}\n")
        
        # 2. Read Agent Specific Context
        directives_file = agent_dir / "core_directives.md"
        if directives_file.exists():
            content = directives_file.read_text(encoding="utf-8").strip()
            if content:
                context_parts.append(f"--- CORE DIRECTIVES ---\n{content}\n")
                
        state_file = agent_dir / "current_state.md"
        if state_file.exists():
            content = state_file.read_text(encoding="utf-8").strip()
            if content:
                context_parts.append(f"--- CURRENT STATE ---\n{content}\n")
                
        if not context_parts:
            return ""
            
        return "\n".join(context_parts)

    def log_task_completion(self, agent_id: str, task_title: str, response: str) -> None:
        """Appends the result of a task to the agent's activity log."""
        try:
            agent_dir = self.ensure_agent_folder(agent_id)
            log_file = agent_dir / "activity_log.md"
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            log_entry = (
                f"\n## Task: {task_title} ({timestamp})\n"
                f"{response}\n"
                f"---\n"
            )
            
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_entry)
                
            logger.info(f"Logged task completion for {agent_id} to Obsidian.")
        except Exception as e:
            logger.error(f"Failed to write activity log for {agent_id}: {e}")
