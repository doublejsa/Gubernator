"""Secret redaction for the audit log.

Pattern-based scrubbing applied to commands/output BEFORE they're stored.
Over-redaction is acceptable here — better to mask a commit hash than leak a key.
The redacted detail is additionally encrypted with the user's vault key, so any
secret that slips past these patterns is still encrypted at rest.
"""
from __future__ import annotations
import re

_SUBS = [
    (re.compile(r'sk-[A-Za-z0-9_\-]{6,}'),                       '[REDACTED-KEY]'),     # anthropic/openai
    (re.compile(r'(?:gh[posru]|github_pat)_[A-Za-z0-9_]{6,}'),   '[REDACTED-TOKEN]'),   # github
    (re.compile(r'glpat-[A-Za-z0-9_\-]{6,}'),                    '[REDACTED-TOKEN]'),   # gitlab
    (re.compile(r'xox[baprs]-[A-Za-z0-9-]{6,}'),                 '[REDACTED-TOKEN]'),   # slack
    (re.compile(r'AKIA[0-9A-Z]{16}'),                            '[REDACTED-AWSKEY]'),  # aws
    (re.compile(r'://[^/\s:@]+:[^/\s@]+@'),                      '://[REDACTED]@'),     # user:pass@host
    (re.compile(r'(?i)(password|passwd|pwd|secret|token|api[_-]?key)(["\']?\s*[=:]\s*["\']?)(\S+)'),
                                                                  r'\1\2[REDACTED]'),    # key=value
    (re.compile(r'(?<=\s)-p\S+'),                                '-p[REDACTED]'),       # mysql -psecret
    (re.compile(r'\b[A-Za-z0-9+/]{40,}={0,2}\b'),                '[REDACTED-BLOB]'),    # long base64
    (re.compile(r'\b[0-9a-fA-F]{40,}\b'),                        '[REDACTED-HEX]'),     # long hex
]

def redact(text: str | None) -> str:
    if not text:
        return ""
    t = text
    for pat, rep in _SUBS:
        t = pat.sub(rep, t)
    return t
