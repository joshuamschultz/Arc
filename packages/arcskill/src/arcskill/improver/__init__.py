"""arcskill.improver — evolutionary skill improvement logic (SPEC-044).

The rightful home for skill-improvement mechanics, beside the hub's signed
install/verify/lock lifecycle. Pure logic over injected Protocol seams
(``Mutator``/``Judge``/``EvalRunner``/``Signer``/``AuditSink``): this subpackage
imports no ``arcagent``/``arcllm``/``arcmemory`` (REQ-004, D-3). arcagent wires
the seams through ``arcagent.skilladapt`` and drives it via the ``SkillAdapter``
Protocol.
"""
