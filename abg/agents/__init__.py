"""Agents for the ABG terminal (docs/17).

* ``abg.agents.lab``     - the Strategy Lab: a deterministic research harness (search space, cached parallel
                           backtests, a validation protocol with a locked holdout, a trial ledger and a proposals
                           queue).  No LLM needed; it is what the agents call.
* ``abg.agents.tools``   - read-only tools over the lab and the bot, as plain functions (+ Strands ``@tool``s).
* ``abg.agents.graph``   - the Strands multi-agent research graph (researcher -> evaluate -> critic -> report).
* ``abg.agents.mcp_server`` - the same tools over MCP (``abg mcp``) for Claude / Cowork / other agents.

Rule of the house: agents PROPOSE, code DECIDES.  Nothing here can place a trade, loosen a loss control, or
change the live bot's settings - a proposal only takes effect when a person approves it (``abg lab approve``).
"""
