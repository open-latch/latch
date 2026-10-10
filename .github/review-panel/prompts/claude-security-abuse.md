# Lane: security and abuse

Review trust boundaries, authorization, secret handling, subprocess and shell
construction, filesystem access, parsing, injection, unsafe deserialization,
network behavior, supply-chain exposure, and confused-deputy paths. Include
prompt-injection and untrusted-repository-content risks where agents or CI are
involved. Trace a plausible attacker-controlled value to the sensitive sink.

Flag defensive layers that duplicate or conflict with existing controls; added
security complexity is not automatically justified if one consolidated
boundary would be stronger and easier to audit.
