# Celery vs RQ for Funding Bot task queue

## Summary

Celery is the recommended replacement for cron in this repository. RQ is easier to learn at first, but Celery is a better fit for the project roadmap because it has a larger Python ecosystem, stronger worker-routing and scheduling support, and better operational tooling for a growing deployment.

## Comparison

| Criterion | Celery | RQ | Recommendation |
| --- | --- | --- | --- |
| Ease of use | More configuration up front, but well understood in Python teams and flexible once set up. | Simpler API and faster first task setup. | RQ wins on simplicity; Celery is still acceptable for this project because the configuration can be standardized in one app module. |
| Scaling | Mature support for multiple workers, queues, routing, retries, beat scheduling, and both Redis and RabbitMQ brokers. | Good for smaller Redis-only job queues, but fewer built-in scaling primitives. | Celery is the better fit for the roadmap beyond a single worker. |
| Monitoring | Strong ecosystem with Flower, broker events, richer worker introspection, and broad community guidance. | Basic dashboard and queue inspection tools, but less depth for larger operations. | Celery provides better observability headroom. |
| Documentation | Extensive official docs, many deployment guides, and a large community footprint. | Clear docs for common cases, but a smaller ecosystem and fewer advanced examples. | Celery has the stronger long-term documentation story. |

## Decision

Choose **Celery** as the primary task queue.

Reasons:

1. The roadmap already calls for scheduled jobs, retries, monitoring, and scalable workers.
2. Celery supports both **Redis** and **RabbitMQ**, letting local development default to Redis while keeping RabbitMQ available for teams that prefer AMQP semantics.
3. Celery has the broader operational ecosystem for future tasks such as Celery Beat scheduling, Flower monitoring, and worker autoscaling.

## Recommended configuration for this repo

- Default broker: `redis://redis:6379/0`
- Default result backend: `redis://redis:6379/1`
- Optional RabbitMQ broker override: `******rabbitmq:5672//`
- App entry point: `celery_app:celery_app`
- Task module: `tasks/celery_tasks.py`

## Migration note

Cron can remain as a fallback during migration, but new asynchronous work should be added to Celery tasks first so the project can move toward workers and scheduled queue-based execution.
