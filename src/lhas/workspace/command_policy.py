from dataclasses import dataclass
@dataclass(frozen=True)
class CommandRule:
    argv_prefix: list[str]
    allow_extra_args: bool = True
    def matches(self, argv): return argv[:len(self.argv_prefix)] == self.argv_prefix and (self.allow_extra_args or len(argv) == len(self.argv_prefix))
class CommandPolicy:
    def __init__(self, rules=()): self.rules=tuple(rules)
    def allows(self, argv): return any(rule.matches(argv) for rule in self.rules)
