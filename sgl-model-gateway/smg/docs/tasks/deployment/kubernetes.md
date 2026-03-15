---
title: Deploy to Kubernetes
---

# Deploy SMG to Kubernetes

This task shows you how to deploy SMG to Kubernetes with service discovery.

<div class="prerequisites" markdown>

#### Before you begin

- Kubernetes cluster (1.24+)
- `kubectl` configured to access your cluster
- Inference workers deployed (or deploy them as part of this task)

</div>

---

## Basic Deployment

### Step 1: Create namespace

```bash
kubectl create namespace inference
```

### Step 2: Deploy SMG

Create the deployment manifest:

```yaml title="smg-deployment.yaml"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: smg
  namespace: inference
  labels:
    app: smg
spec:
  replicas: 1
  selector:
    matchLabels:
      app: smg
  template:
    metadata:
      labels:
        app: smg
    spec:
      containers:
        - name: smg
          image: lightseekorg/smg:latest
          ports:
            - containerPort: 30000
              name: http
            - containerPort: 29000
              name: metrics
          args:
            - --worker-urls
            - http://sglang-worker:8000
            - --policy
            - cache_aware
            - --host
            - "0.0.0.0"
            - --port
            - "30000"
            - --prometheus-port
            - "29000"
          livenessProbe:
            httpGet:
              path: /health
              port: 30000
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /readiness
              port: 30000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: "1"
              memory: "1Gi"
            limits:
              cpu: "4"
              memory: "4Gi"
---
apiVersion: v1
kind: Service
metadata:
  name: smg
  namespace: inference
spec:
  selector:
    app: smg
  ports:
    - name: http
      port: 80
      targetPort: 30000
    - name: metrics
      port: 9090
      targetPort: 29000
  type: ClusterIP
```

Apply the manifest:

```bash
kubectl apply -f smg-deployment.yaml
```

### Step 3: Verify deployment

```bash
# Check pods
kubectl get pods -n inference -l app=smg

# Check service
kubectl get svc -n inference smg

# Check logs
kubectl logs -n inference -l app=smg
```

---

## Service Discovery

Enable automatic worker discovery from Kubernetes pods.

### Step 1: Create ServiceAccount and RBAC

```yaml title="smg-rbac.yaml"
apiVersion: v1
kind: ServiceAccount
metadata:
  name: smg
  namespace: inference
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: smg
  namespace: inference
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: smg
  namespace: inference
subjects:
  - kind: ServiceAccount
    name: smg
    namespace: inference
roleRef:
  kind: Role
  name: smg
  apiGroup: rbac.authorization.k8s.io
```

```bash
kubectl apply -f smg-rbac.yaml
```

### Step 2: Update deployment for service discovery

```yaml title="smg-discovery.yaml"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: smg
  namespace: inference
spec:
  template:
    spec:
      serviceAccountName: smg
      containers:
        - name: smg
          args:
            - --service-discovery
            - --selector
            - app=sglang-worker
            - --service-discovery-namespace
            - inference
            - --service-discovery-port
            - "8000"
            - --policy
            - cache_aware
            - --host
            - "0.0.0.0"
```

### Step 3: Label your workers

Ensure your worker pods have matching labels:

```yaml
metadata:
  labels:
    app: sglang-worker
```

---

## High Availability

Deploy multiple SMG replicas for high availability.

### Step 1: Scale replicas

```yaml
spec:
  replicas: 3
```

### Step 2: Add pod anti-affinity

```yaml
spec:
  template:
    spec:
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: smg
                topologyKey: kubernetes.io/hostname
```

### Step 3: Configure HPA

```yaml title="smg-hpa.yaml"
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: smg
  namespace: inference
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: smg
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

---

## Ingress

Expose SMG externally.

```yaml title="smg-ingress.yaml"
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: smg
  namespace: inference
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "100m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "600"
spec:
  ingressClassName: nginx
  rules:
    - host: inference.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: smg
                port:
                  number: 80
  tls:
    - hosts:
        - inference.example.com
      secretName: smg-tls
```

---

## Verification

```bash
# Check all resources
kubectl get all -n inference -l app=smg

# Port forward for testing
kubectl port-forward -n inference svc/smg 30000:80

# Test endpoint
curl http://localhost:30000/health

# Check discovered workers
curl http://localhost:30000/workers
```

---

## Troubleshooting

??? question "Service discovery not finding workers"

    1. Check RBAC permissions:
    ```bash
    kubectl auth can-i list pods -n inference --as system:serviceaccount:inference:smg
    ```

    2. Verify labels match:
    ```bash
    kubectl get pods -n inference -l app=sglang-worker
    ```

    3. Check SMG logs:
    ```bash
    kubectl logs -n inference -l app=smg | grep -i discovery
    ```

??? question "Pods not becoming ready"

    Check readiness probe:
    ```bash
    kubectl describe pod -n inference -l app=smg
    ```

    Verify workers are accessible:
    ```bash
    kubectl exec -n inference -it <smg-pod> -- curl http://sglang-worker:8000/health
    ```

---

## What's Next?

- [Configure TLS](tls.md) — Secure communications
- [Monitor with Prometheus](../operations/monitoring.md) — Set up observability
