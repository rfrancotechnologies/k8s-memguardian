#!/usr/bin/env python
import argparse
import logging
import os
import re
import random
import sys
import time
import pprint
import datetime
from collections import Counter

import kubernetes
import prometheus_client as prometheus

logger = logging.getLogger(__name__)


def configure_logging(verbosity):
    msg_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    VERBOSITIES = [logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG]
    level = VERBOSITIES[min(int(verbosity), len(VERBOSITIES) - 1)]
    formatter = logging.Formatter(msg_format)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)


def parse_args():
    parser = argparse.ArgumentParser(description="Memory Guardian for Kubernetes PODs")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase verbosity"
    )
    parser.add_argument(
        "--kubeconfig",
        default=os.getenv("KUBECONFIG", ""),
        help="Path to kubeconfig file.",
    )
    parser.add_argument(
        "--incluster-base-path",
        default=os.getenv("SCHED_INCLUSTER_BASE_PATH", ""),
        help="Path to directory containing the token.",
    )
    parser.add_argument(
        "-d", "--daemon", default=False, action="store_true", help="Run forever"
    )
    parser.add_argument(
        "--delay",
        default=float(os.getenv("SCHED_DELAY", 10)),
        type=float,
        help="time to wait between loops",
    )
    parser.add_argument(
        "--prometheus-port",
        default=int(os.getenv("SCHED_PROMETHEUS_PORT", 8000)),
        type=int,
        help="Prometheus Exporter port",
    )
    parser.add_argument(
        "--prometheus-disable",
        action="store_true",
        default=os.getenv("SCHED_PROMETHEUS", "true") == "false",
        help="Prometheus Exporter disable",
    )
    result = parser.parse_args()

    return result


def metric_to_bytes(amount):
    multipliers = {
        '': 1,
        'k': 1000,
        'm': 1000000,
        'g': 1000000000,
        't': 1000000000000,
        'p': 1000000000000000,
        'e': 1000000000000000000,
        'ki': 1024,
        'mi': 1024 * 1024,
        'gi': 1024 * 1024 * 1024,
        'ti': 1024 * 1024 * 1024 * 1024,
        'pi': 1024 * 1024 * 1024 * 1024 * 1024,
        'ei': 1024 * 1024 * 1024 * 1024 * 1024 * 1024,
    }
    m = re.match("(?P<amount>\d+\.?\d*)(?P<units>\w*)", amount)
    return int(m.group('amount')) * multipliers.get(m.group('units').lower(), 0)


class Container:
    annotation_string = "memguardian.limit.memory"

    def __init__(self, pod_metadata, container_spec):
        self.pod_metadata = pod_metadata
        self.container_spec = container_spec
        self.fullname= self.gen_key(pod_metadata, container_spec)

    @classmethod
    def gen_key(cls, pod_metadata, container_spec):
        namespace = pod_metadata['namespace'] if isinstance(pod_metadata, dict) else pod_metadata.namespace
        podname = pod_metadata['name'] if isinstance(pod_metadata, dict) else pod_metadata.name
        name = container_spec['name'] if isinstance(container_spec, dict) else container_spec.name
        return f'{namespace}.{podname}.{name}'

    @property
    def namespace(self):
        return self.pod_metadata.namespace

    @property
    def podname(self):
        return self.pod_metadata.name

    @property
    def podfullname(self):
        return f"{self.pod_metadata.namespace}.{self.pod_metadata.name}"

    @property
    def name(self):
        return self.container_spec.name

    def __str__(self):
        return self.fullname

    @property
    def controller(self):
        for owner in self.pod_metadata.owner_references:
            if owner.controller:
                return owner

    @property 
    def controller_string(self):
        controller = self.controller
        return f'{controller.kind}/{controller.name}'

    @property
    def memory_limit(self):
        annotations = self.pod_metadata.annotations or {}
        for key in (f'{self.annotation_string}/{self.name}', self.annotation_string):
            value = annotations.get(key)
            if value is not None:
                return metric_to_bytes(value)
        return None


class MemGuardian:
    def __init__(self, kubernetes_client):
        self.kclient = kubernetes_client
        self.limits_gauge = prometheus.Gauge(
            "memguardian_config_limits",
            "Total deleted pods from start.",
            ["namespace"],
        )

    def _limited_containers(self):
        namespaced_limits = Counter()
        for pod in self.kclient.get_pods():
            metadata = pod.metadata

            for container_spec in pod.spec.containers:
                container = Container(metadata, container_spec)
                limit = container.memory_limit
                if limit is None:
                    continue
                logger.debug("Found limit %s -> %s", container.fullname, limit)
                namespaced_limits.update([container.namespace])
                yield container
        for namespace, limits in namespaced_limits.items():
            self.limits_gauge.labels(namespace=namespace).set(limits)

    def run(self, dry=False):
        limited_containers = dict((x.fullname, x) for x in self._limited_containers())
        updated_controllers = []

        for metric in self.kclient.get_metrics():
            metadata = metric['metadata']
            for container_metric in metric['containers']:
                value = metric_to_bytes(container_metric['usage']['memory'])
                name = container_metric["name"]
                key = Container.gen_key(metadata, container_metric)
                container = limited_containers.get(key)
                if container is None:
                    continue

                memory_limit = container.memory_limit
                if value > memory_limit:
                    logger.debug(
                        "Container %s reached the limit: %s > %s and should be removed .",
                        key,
                        value,
                        memory_limit,
                    )
                    if container.controller_string in updated_controllers:
                        logger.debug(f"Controller for container {container} has another pod removed this loop. Skipping.")
                        continue

                    self.delete_container(container)
                    updated_controllers.append(container.controller_string)

    def delete_container(self, container):
        logger.debug(f"Checking if {container} can be removed...")
        controller_spec = container.controller
        if controller_spec is None:
            logger.debug(f"Container {container} has no controller, so it cannot be removed.")
            return
        controller = self.kclient.read_namespaced_resource_status(
            controller_spec.name,
            container.namespace,
            controller_spec.kind,
        )
        status = controller.status
        if (status.ready_replicas or 0) < (status.replicas or 1000):
            logger.warning(f"Container {container} might contain unready siblings, so it won't be deleted")
            return
        logger.debug(f"Removing pod with {container.podfullname}")
        self.kclient.delete_namespaced_pod(container.podname, container.namespace, container.controller_string)


class KubernetesClient:
    def __init__(self, kubeconfig, token_path):
        token_file = None
        token_path = token_path or "/var/run/secrets/kubernetes.io/serviceaccount"
        if kubeconfig and os.path.exists(kubeconfig):
            logger.debug("Using configuration from kubeconfig %s" % kubeconfig)
            kubernetes.config.load_kube_config(config_file=kubeconfig)
        elif os.path.exists(token_path):
            logger.debug("Using configuration from token in %s" % token_path)
            loader = kubernetes.config.incluster_config.InClusterConfigLoader(
                os.path.join(token_path, "token"), os.path.join(token_path, "ca.crt"),
            )
            loader.load_and_set()
        else:
            raise Exception("No kubeconfig or token found")

        if token_file:
            loader = kubernetes.config.incluster_config.InClusterConfigLoader(
                "/var/run/secrets/kubernetes.io/serviceaccount/token",
                "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            )
            configuration = kubernetes.client.Configuration()
            configuration.host = "https://192.168.1.10:6443"
            loader.load_and_set()

        self.v1 = kubernetes.client.CoreV1Api()
        self.appsv1 = kubernetes.client.AppsV1Api()
        self.client = kubernetes.client.ApiClient()
        self.custom = kubernetes.client.CustomObjectsApi()

        self.deleted_total = prometheus.Counter(
            "memguardian_deleted_pod_total",
            "Total deleted pods from start.",
            ["namespace", "owner"],
        )

    def get_nodes(self):
        return self.v1.list_node().items

    def get_pods(self):
        return self.v1.list_pod_for_all_namespaces().items

    def get_metrics(self):
        metrics = self.custom.list_cluster_custom_object('metrics.k8s.io', 'v1beta1', 'pods')
        return metrics['items']

    def delete_namespaced_pod(self, name, namespace, owner):
        self.v1.delete_namespaced_pod(name, namespace)
        self.deleted_total.labels(namespace=namespace, owner=owner).inc()

    def read_namespaced_resource_status(self, name, namespace, kind):
        logger.debug(f"Retrieving controller {name} with type {kind} in {namespace}")
        controllers = {
            'deployment': self.appsv1.read_namespaced_deployment_status,
            'statefulset': self.appsv1.read_namespaced_stateful_set_status,
            'replicationcontroller': self.v1.read_namespaced_replication_controller_status,
            'replicaset': self.appsv1.read_namespaced_replica_set_status,
        }
        fn = controllers.get(kind.lower())
        return None if fn is None else fn(name, namespace) 


def main():
    args = parse_args()
    configure_logging(args.verbose)
    logger.debug("Arguments: {args}".format(args=args))

    if not args.prometheus_disable:
        logger.debug(
            "Starting prometheus exporter on {port}".format(port=args.prometheus_port)
        )
        prometheus.start_http_server(args.prometheus_port)

    kclient = KubernetesClient(args.kubeconfig, args.incluster_base_path)
    memguardian = MemGuardian(kclient)

    run_time_summary = prometheus.Summary(
        "memguardian_loop",
        "Loop execution time",
    )
    exceptions_count = prometheus.Counter(
        "memguardian_error",
        "Errors in the main loop",
    )
    while True:
        logger.debug("Running MemGuardian")
        try:
            with exceptions_count.count_exceptions():
                with run_time_summary.time():
                    memguardian.run()
        except:
            logger.exception("Unknown problem in the run loop.")
        if not args.daemon:
            break
        logger.debug(
            "MemGuardian finished. Sleeping for {delay}".format(delay=args.delay)
        )
        time.sleep(args.delay)


if __name__ == "__main__":
    main()
