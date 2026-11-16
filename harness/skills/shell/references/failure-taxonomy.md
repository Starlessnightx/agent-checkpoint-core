# Shell failure taxonomy

| Failure kind | Evidence | First discriminating action |
|---|---|---|
| missing command | shell reports executable not found | inspect project metadata and PATH |
| dependency | import/package/library error | locate declaring lock/config file |
| permission | denied path, bind, or executable | inspect exact target permissions/ownership |
| syntax | parser/compiler location | read referenced region and nearby context |
| assertion | explicit expected/actual mismatch | map assertion to requested contract |
| timeout | deadline with partial output | decide foreground vs background and inspect progress |
| signal | negative/terminated status | inspect signal and resource/log evidence |
| network | connection, DNS, TLS, registry failure | distinguish external infrastructure from product behavior |
| unknown | none of the above | preserve complete artifact and form a falsifiable hypothesis |

Retryable describes infrastructure likelihood, not guaranteed safety. A retryable
network failure may be retried with the same inputs once; a deterministic syntax
failure requires changed code or arguments.
