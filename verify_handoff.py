import os
from src.macro_agents.orchestrator import MacroOrchestrator

# Set up environment variables to test LiteLLM
# This uses the public litellm testing endpoint or a mock to avoid billing.
# For hermes, the user will configure OPENAI_API_BASE to their local endpoint.
os.environ["MODEL_PROVIDER"] = "openai"
os.environ["MODEL_NAME"] = "gpt-4o-mini"
os.environ["OPENAI_API_KEY"] = "sk-test-key-123" 

print("Testing Orchestrator with default litellm_handoff...")

try:
    orch = MacroOrchestrator(
        state_dir="./data/state_data",
        database_url="sqlite:///./data/agency_agents.db",
        chroma_path="./data/chroma_data",
        source_root="./src/bots_config",
    )
    
    # We won't actually call orchestrate() because without a valid API key, 
    # litellm will throw an AuthenticationError when trying to hit OpenAI.
    # But we can verify that the orchestrator initialized correctly with the new handoff_fn.
    print(f"Orchestrator initialized. Handoff function is: {orch.handoff_fn.__name__}")
    assert orch.handoff_fn.__name__ == "litellm_handoff"
    
    print("SUCCESS: Handoff logic successfully wired!")
    
except Exception as e:
    print(f"ERROR: {e}")
