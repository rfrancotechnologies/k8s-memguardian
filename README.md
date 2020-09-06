# Memory Guardian for Kubernetes

MemGuardian will check the memory consumed by containers periodically. If the POD has some annotations, it will delete the pod when it requires more memory than configured.

This might sound like a memory limit, but there are several differences:

- Limits consider each POD individually. MemGuardian will not delete a POD unless every POD in the same controller (replicaset, daemonset, ...) are in READY status, reducing the DoS (Denial of Service) probability.
- Limits perform a `kill -9`, which allows no control. MemGuardian will delete a POD in the same way as `kubectl delete pod`, which performs a `kill -15` and wait configured grace periods and so on.
- In addition, MemGuardian won't remove more than one POD per controller in each loop to avoid DoS. It prefers to retain the PODs for longer than create DoS.

# Usage

Just deploy the MemGuardian (with appropiate permision, see `example/rbac.yaml`) and add annotations like these to your desired pods, throught deployment templates or however:
- `memguardian.limit.memory: 1000000` Limits the memory in any container in the pod to 1000000 bytes
- `memguardian.limit.memory: 1000k` Limits the memory in any container in the pod to 1000000 bytes
- `memguardian.limit.memory: 1m` Limits the memory in any container in the pod to 1000000 bytes
- `memguardian.limit.memory: 1Mi` Limits the memory in any container in the pod to 1 Mebibyte. 
- `memguardian.limit.memory/nginx: 3Mi` Limits the memory of "nginx" container in the pod to 3 Mebibytes. 

# known issues

Currently it is retrieving all pods status on each loop. In big Kubernetes deployments this might require too many resources or be slow.

# Testing it locally

## Kind

You can use [Kind](https://kind.sigs.k8s.io/) to test it. You have a configuration file at `example/kind.yaml` that can be used with:

```
kind create cluster --config example/kind.yaml
```

## Metrics-server

In the `example` directory there are some files than might be useful. Anyways, it is required to have a metrics-server running. The easiest way is to run:

```
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.3.7/components.yaml
```

And maybe edit the metrics-server deployment to add these arguments:

```
--kubelet-insecure-tls=true
--kubelet-preferred-address-types=InternalIP
```

With this, any call `kubectl top pods` should return values.

Check the [metrics-system documentation](https://github.com/kubernetes-sigs/metrics-server) for more information.


## Ingress

If you are going to use Kind to test it, maybe you want to enable Ingress. 
This is not a required step, but might be useful to test if your services are having DoS.
To do it, you can use the file `example/kind.yaml` and configure nginx as ingress backend:

```
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/master/deploy/static/provider/kind/deploy.yaml
```

More information at [kind ingress documentation](https://kind.sigs.k8s.io/docs/user/ingress/).

You will require the files:
- example/ingress.yaml
- example/service.yaml


## Other useful files

There are two files proposed to test a deployment or a statefulset:

- example/deploy.yaml
- example/statefulset.yaml

The previously configured service, found at `example/service.yaml` will search for any label `app: nginx`, which are met both for the deployment and the statefulset, so please, use only of them at a time.

## Running it

### Outside of Kubernetes

If you already have your Kind and Metrics-Server configured (remember Ingress is optional), you can just run memGuardian:

```
./memguardian.py --prometheus-port 10000 -d -vvvvv
```
You can ask the service at port 10000 for Prometheus metrics.

### Inside of Kubernetes

You can run MemGuardian inside Kubernetes, but you require permission.

To allow this, there are a couple of example files:

- example/rbac.yaml
- example/memguardian-deployment.yaml

The former creates all the permission resources required by memguardian and the latter creates the MemGuardian deployment.


