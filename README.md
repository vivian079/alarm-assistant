# Alarm Handling Assistant

Input one semiconductor equipment alarm record, get a handling plan that an equipment engineer reviews.
All data in `data/` is synthetic.

Current stage: Day 2 MVP. One synchronous endpoint, the prompt only gets the alarm record (no SOP text yet).

## Run

```
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
docker compose up --build
```

- Page: http://localhost:8501
- API docs: http://localhost:8000/docs
