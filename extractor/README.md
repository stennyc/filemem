# Filemem Extractor

Extracts structured memories from conversation messages.

## Usage

```python
from plugins.filemem.extractor.memory_extractor import MemoryExtractor

extractor = MemoryExtractor()
memories = extractor.extract(messages)
```

## Extraction Rules

1. **Preferences**: Look for patterns like "I prefer", "I like", "I don't like", "always", "never"
2. **Facts**: Declarative statements from user (end with period, >20 chars)
3. **Decisions**: Solutions to problems, configuration choices
4. **Tasks**: Action items, TODOs, future plans
