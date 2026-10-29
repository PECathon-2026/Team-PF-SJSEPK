DevPilot: The Mystic Mikado’s Autonomous AI Engineering Guild
Background
When a decree arrives in the Emperor’s kingdom, his engineering guild of strategists, builders,
testers, and guardians must plan, build, verify, and review the change, and adapt when things
go wrong. Build DevPilot, a multi-agent AI system that turns a natural-language software
request into a working, tested, and reviewed change to an existing small repository.
The core principle is: Plan like a Shogun. Build like a Samurai. Test like a Sentinel. Adapt until
the mission is complete.
Tasks
A) The Shogun’s Council: Planning
● Requirement Analysis: Convert the request into objectives and acceptance criteria.
● Codebase Reconnaissance: Inspect the project to find relevant files, conventions, and
risks.
● Task Decomposition and Delegation: Break the mission into tasks, assign them to
agents, and revise the plan when new information or blockers appear.
● Shared Mission State: Keep a common record of task status, findings, decisions, and
open issues across agents.
B) The Samurai Forge: Implementation
● Tool Selection: Developer agents choose among at least three tools (e.g., file
read/search, file edit, terminal commands).
● Context-Aware Development: Follow the project’s existing style and structure rather
than blindly generating new code.
● Adaptive Problem Solving: Investigate unexpected errors and change approach when
needed.
● Controlled Execution: Restrict commands and file access to the repo folder, and block
destructive operations.
C) The Sentinel Dojo: Testing and Verification
● Automated Testing: Run the project’s tests (or build/lint checks).
● Failure Investigation and Repair: Analyze errors, send findings back to the developer
agent, and rerun.
● Independent Verification: A separate verifier agent or process checks the acceptance
criteria.● Evidence-Based Completion: Record what changed, which checks passed or failed,
and what remains unresolved. The system must not claim success without evidence.
D) The Shadow Council: Review and Security
● Code Review: Review the changes for correctness, readability, consistency, and
regressions.
● Security Inspection: Run basic checks for exposed secrets, unsafe operations, and
injection risks.
● Cross-Agent Critique: Reviewers can send findings back to developers.
Disagreements are settled by evidence or escalated to a human.
● Human Oversight: Require human approval before consequential actions such as
merging or applying changes.
E) The Living Campaign: Coordination and Adaptive Autonomy
● Inter-Agent Communication: Agents exchange findings and use each other’s results.
Sequential LLM calls with diferent personas do not count.
● Feedback Loops: Failures or review findings must trigger further investigation and
revised actions.
● Adaptive Orchestration: Decide when to continue, retry, ask for clarification, or stop.
● Bounded Autonomy: Define success conditions, iteration limits, permissions, and
escalation rules. Handle failed tools and repeated failures without endless loops.
The architecture, number of agents, and division of responsibilities are up to you.
Technical Requirements
Use any language, framework, AI model, or agent framework. Existing models, libraries, APIs,
and tools are allowed. A local repository is enough, and GitHub is not required. The goal is not
to maximize the number of agents or tools, but to show that coordination and iterative reasoning
contribute meaningfully to the result.
Evaluation Scenarios
Test the system on small controlled repositories covering:
● Feature Implementation: Add a feature while preserving existing behavior.
● Bug Investigation: Fix a seeded defect using repository context and test output.
● Test-Driven Recovery: Repair a repository that has a failing test.
● Security and Code Review: Detect and address a planted vulnerability or quality issue.
● Unexpected Conditions: Handle an unavailable tool, an ambiguous requirement, or
repeated test failures.Demonstrate at least one complete end-to-end mission and one feedback loop in which new
evidence changes what an agent does next.
Evaluation Criteria
● Task Completion: Does the change satisfy the acceptance criteria?
● Agent Coordination: Do agents share findings and respond to each other’s results?
● Tool-Calling Intelligence: Are tools chosen appropriately for the situation?
● Adaptability: Can the system recover from failures and revise its plan?
● Code Quality: Are the changes correct, maintainable, and consistent with the project?
● Testing and Verification: Are results backed by real test runs and independent checks?
● Security and Reliability: Are permissions, boundaries, and failure conditions handled
responsibly?
● Transparency: Can users inspect the plan, agent decisions, tool use, and test results?
All reported results must come from actual test runs. Clearly separate verified outcomes from
assumptions and model claims.
Deliverables
● Source code and a working demonstration
● Multi-agent architecture with defined agent roles
● Working implementation, testing, and verification workflow
● At least one end-to-end mission with a run log showing a feedback loop
● Setup instructions, configuration files, and a list of supported tools
Documentation (README.md)
Include:
● Architecture diagram and agent responsibilities
● Agent communication, orchestration, and shared-state design
● Setup instructions and configuration
● Available tools and execution boundaries
● Human approval mechanism, security assumptions, and known limitations
Bonus Points
● Parallel or dynamically created agents
● Automatic reruns and self-correction when tests fail
● Persistent memory, or comparison against a single-agent baseline
Expected OutcomeA functional multi-agent system that interprets a development request, plans the work,
coordinates agents, uses tools, implements changes, responds to failures, and verifies the
result. It should not be a fixed pipeline of chatbots. Let the guild prove its worth not by the
number of agents it commands, but by the quality of the software it delivers