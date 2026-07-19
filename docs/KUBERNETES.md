# Kubernetes deployment guide

This guide describes the dashboard-focused Kubernetes manifests under `k8s/`.

## Manifests

- `k8s/deployment.yaml` — dashboard `Deployment` with requests/limits and health probes
- `k8s/service.yaml` — internal `ClusterIP` service on port `80`
- `k8s/ingress.yaml` — external HTTP(S) routing for the dashboard
- `k8s/hpa.yaml` — horizontal pod autoscaling from CPU and memory usage
- `k8s/vpa.yaml` — vertical recommendations applied when new pods start
- supporting resources: `namespace.yaml`, `configmap.yaml`, `secret.yaml`, `persistentvolumeclaim.yaml`, `cronjob.yaml`

## Prerequisites

1. A Kubernetes cluster with the metrics server installed for HPA.
2. The Vertical Pod Autoscaler controller installed if you want `k8s/vpa.yaml` to take effect.
3. An ingress controller such as NGINX for `k8s/ingress.yaml`.
4. A persistent storage class for `k8s/persistentvolumeclaim.yaml`.

## Configure before deploy

Update these placeholders before applying the manifests:

- `k8s/deployment.yaml`: replace `ghcr.io/example/funding-bot:v1.0.0` with your published image tag or digest.
- `k8s/ingress.yaml`: replace `funding-bot.example.com` and `funding-bot-tls`.
- `k8s/secret.yaml`: replace dashboard and SMTP placeholders.
- `k8s/configmap.yaml`: set queue mode and SMTP/runtime values for your environment.
- `k8s/persistentvolumeclaim.yaml`: tune storage class/size if your cluster requires it.

## Apply the manifests

Create the namespace first, then apply the remaining resources:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/persistentvolumeclaim.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/vpa.yaml
kubectl apply -f k8s/cronjob.yaml
```

## Health checks

The dashboard exposes `GET /health`, which is used for:

- `startupProbe` to delay restarts until Flask is serving
- `readinessProbe` to keep pods out of service until healthy
- `livenessProbe` to restart unhealthy pods

## Autoscaling strategy

### HPA

`k8s/hpa.yaml` scales the dashboard between `2` and `6` replicas when average utilization rises above:

- `70%` CPU
- `75%` memory

### VPA

`k8s/vpa.yaml` runs in `Initial` mode so recommendations are applied on fresh pods without continuously changing running pods. This avoids HPA/VPA contention while still helping tune requests and limits over time.

## Verification

```bash
kubectl get deploy,svc,ingress,hpa,vpa -n funding-bot
kubectl rollout status deployment/funding-bot -n funding-bot
kubectl describe hpa funding-bot -n funding-bot
kubectl get pods -n funding-bot
```

Check the dashboard health endpoint through the service or ingress after rollout:

```bash
kubectl port-forward -n funding-bot svc/funding-bot 8080:80
curl http://127.0.0.1:8080/health
```
