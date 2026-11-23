# Grounding LLM Engineering Reports in a Causal Knowledge Layer

**The problem.** A large language model can write a fluent incident report, but on its own it has no access to *this* pump's
nameplate, alarm limits, sensor history or failure physics. Asked "why is P-101 vibrating?", it will produce a plausible
list of generic causes. Some of them may be impossible for this machine, and it may state values that were never
measured. In maintenance that is dangerous: a confident but wrong root cause sends a crew to replace a bearing when the
real problem is a fouled suction strainer.

**The idea: let the graph reason and the LLM write.** The knowledge layer splits the work between a deterministic engine and the
language model. The ontology fixes the vocabulary: Equipment, Component, Sensor, Alarm, Symptom, FailureMode, RootCause and
Action, with typed relations validated against a schema. The causal graph encodes physics as explicit, reviewable paths,
for example `RC-STRAINER-CLOGGED -causes-> FM-CAVITATION -manifests_as-> SYM:npsh_margin:LOW -observed_by-> NPSH-101`.
`diagnose_fault()` produces the *conclusion* deterministically: it derives physical quantities (NPSH margin, head ratio
against the OEM curve), matches symptom signatures, and isolates root causes with explicit statuses (SUPPORTED, UNVERIFIED,
RULED_OUT). The LLM is then asked only to *explain and communicate* that result, not to diagnose. This is
Retrieval-Augmented Generation in which the retrieved knowledge is a subgraph rather than text chunks (often called
GraphRAG).

**The pipeline** (implemented in `llm_context.py`):

1. **Detect.** `diagnose_fault(sensor_states)` returns ranked failure modes, evidence, causal chains and actions.
2. **Retrieve.** `retrieve_subgraph()` collects only the nodes relevant to this diagnosis: the failure modes, their
   symptoms and sensors, candidate root causes and their locations, and the actions. For the clogged-strainer case that
   is about 50 triples out of 270. Irrelevant knowledge (for example motor overload) is left out, so the prompt stays
   small and on-topic.
3. **Serialise.** `build_llm_context()` renders the snapshot, physics-derived values with their formulas, alarms, the
   mechanism text of each failure mode, and the retrieved triples. Every fact carries its node id.
4. **Constrain.** The system prompt requires the model to use only the supplied facts, cite node ids for every claim, keep the
   engine's confidence values, never upgrade an UNVERIFIED cause, and name any missing measurement.
5. **Generate and verify.** The LLM writes the report. Because every claim cites an id, a post-check can reject any sentence
   whose id is not in the retrieved subgraph.

**Why the structure matters more than the model.**
- *Factual grounding.* Values, limits and asset paths come from the graph, not from the model's memory.
- *Physics, not correlation.* The graph contains causal mechanisms. A prompt that says the motor current is LOW *because*
  centrifugal-pump power is lowest at shut-off lets the model explain deadheading correctly. Left unguided, it would likely
  claim the opposite.
- *Calibrated uncertainty.* Statuses such as UNVERIFIED, together with the list of unmeasured key symptoms, give the model the
  words to express doubt honestly. An example: "lubrication failure is the most likely cause but cannot be confirmed without
  oil analysis."
- *Negative knowledge.* Ruled-out hypotheses with reasons ("cavitation ruled out: NPSH margin NORMAL") stop the model from
  suggesting them again.
- *Traceability and audit.* Each sentence maps to a graph path, so an engineer can verify the report in minutes. This
  supports management-of-change and incident-review requirements.
- *Model independence.* The prompt is plain text, so the same layer can feed any provider or an on-premise model.

**Scaling.** The same ontology covers a fleet: new assets add instances, not code. Over time the graph can also index
unstructured sources such as OEM manuals, past work orders and RCA reports, attached to the FailureMode or Component nodes
they describe. Graph retrieval then brings in exactly the manual section or past incident linked to the diagnosed failure
mode, and the root-cause `prior` values can be learned from CMMS history instead of engineering judgement.

**Limitations.** Grounding reduces hallucination but does not remove it, so the citation check and human sign-off remain
essential for safety-relevant actions. The diagnosis is only as good as the encoded signatures, so the graph must be
maintained like any other engineering document.
