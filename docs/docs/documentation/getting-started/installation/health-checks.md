# Health Checks

Mealie exposes two unauthenticated endpoints with empty response bodies and
`Cache-Control: no-store`:

| Endpoint | Success | Failure | Purpose |
| --- | --- | --- | --- |
| `/api/app/health/live` | `200` | HTTP timeout if the application cannot respond | Liveness: checks the application without calling the database or external services. |
| `/api/app/health/ready` | `200` | `503` | Readiness: checks that the application can acquire a database connection and read its users table. |

Readiness waits up to three seconds for the database check. Checks share at most
one in-flight database operation per application worker, so a stalled driver does
not accumulate additional database calls or consume the HTTP worker pool. An HTTP
timeout does not cancel database I/O: a stalled operation can remain in flight
until the driver returns. Subsequent requests remain bounded and report `503`;
new checks resume once that operation finishes. Responses never include database
errors or credentials. Readiness does not verify every feature or database writes.

The `/api/app/about` endpoint remains available for application information.

## Database Startup

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DB_STARTUP_TIMEOUT_SECONDS` | `10` | Maximum time to wait for database connectivity before exiting startup with an error. |
| `DB_STARTUP_RETRY_INTERVAL_SECONDS` | `1` | Delay after a failed connectivity check, capped by the remaining startup budget. |

Both values accept positive, finite numbers, including fractional seconds.
Previously startup made ten connection attempts with one-second sleeps, in
addition to however long database calls took. The timeout now limits the total
connectivity wait, including a stalled connection attempt. Increase it if your
database regularly takes longer to become available.

The budget excludes migrations and other initialization. Migration failures stop
startup; migrations are not retried by the connectivity loop. Mealie begins
serving HTTP after application initialization finishes.

## Docker

The image health check uses readiness, respects `API_PORT` and the existing TLS
settings, and limits curl to five seconds. The image allows a 60-second startup
grace period before health-check failures count. Override Docker's health-check
`start_period` for installations with longer initialization or migrations.

## Kubernetes

Use readiness to remove an unavailable pod from Service traffic and liveness to
restart an unresponsive application. A database outage after startup should fail
readiness without causing liveness restarts. A startup probe prevents liveness
checks from interrupting initialization.

This is a fragment for the Mealie container in a Deployment:

```yaml
env:
  - name: DB_STARTUP_TIMEOUT_SECONDS
    value: "60"
  - name: DB_STARTUP_RETRY_INTERVAL_SECONDS
    value: "1"
startupProbe:
  httpGet:
    path: /api/app/health/ready
    port: 9000
  periodSeconds: 5
  timeoutSeconds: 5
  failureThreshold: 60
livenessProbe:
  httpGet:
    path: /api/app/health/live
    port: 9000
  periodSeconds: 10
  timeoutSeconds: 2
  failureThreshold: 3
readinessProbe:
  httpGet:
    path: /api/app/health/ready
    port: 9000
  periodSeconds: 5
  timeoutSeconds: 5
  failureThreshold: 2
```

This allows approximately five minutes for startup. Size that allowance to cover
the configured database wait **plus** initialization and migrations. A startup
probe does not extend Mealie's own database timeout. Change all probe ports when
using a custom `API_PORT`, and set `scheme: HTTPS` on each `httpGet` if Mealie itself
serves TLS. TLS terminated at an ingress does not require HTTPS probes to the pod.

Kubernetes probes must be configured explicitly; they do not use the image's
Docker health check. These endpoints do not make multiple replicas or concurrent
migrations safe. See the [Kubernetes probe documentation](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/).
