#!/usr/bin/env python

import os
import sys
import json
import random
import time
from datetime import datetime, timezone
from dateutils import relativedelta
import argparse
import traceback
from kubernetes import client, config

DEFAULT_RETENTION_DAYS = os.environ.get('DEFAULT_RETENTION_DAYS', 14)

parser = argparse.ArgumentParser(description='Create EC2 snapshots for kubernetes PVs.')
parser.add_argument('--dont-create-snapshots', dest='create_snapshots', action='store_false', default=True,
                    help='Don\'t clean old snapshots.')
parser.add_argument('--retention-days', dest='retention_days', type=int, default=DEFAULT_RETENTION_DAYS,
                    help='How many days should old snapshots be stored. Older than that will be deleted.')
parser.add_argument('--dont-clean-old-snapshots', dest='clean_old_snapshots', action='store_false', default=True,
                    help='Don\'t clean old snapshots.')
parser.add_argument('--dry-run', dest='dry_run', action='store_true', default=False,
                    help='Don\'t save a bit')

args = parser.parse_args()

if args.retention_days < 1:
    args.retention_days = DEFAULT_RETENTION_DAYS
    print(f'Invalid retention days. Reseted to default: {args.retention_days}')

if os.environ.get('AWS_ACCESS_KEY_ID') and os.environ.get('AWS_SECRET_ACCESS_KEY'):
    print('--> Detected provider AWS')
    from providers.aws import AWS
    provider = AWS()
elif os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') and os.environ.get('GOOGLE_ZONE'):
    print('--> Detected provider GCE')
    with open(os.environ['GOOGLE_APPLICATION_CREDENTIALS']) as credentials:
        project = json.load(credentials)["project_id"]

    from providers.gce import GCE
    provider = GCE(project, os.environ.get('GOOGLE_ZONE'))
else:
    print('--> Unable to detected provider')
    sys.exit(0 if args.dry_run else 1)

print('--> Started', datetime.utcnow())

if 'KUBERNETES_SERVICE_HOST' in os.environ:
    config.load_incluster_config()
else:
    config.load_kube_config()

corev1 = client.CoreV1Api()


def gen_event(corev1, pv, err=""):
    claim_ref = get_claim_ref(pv)
    hostname = os.environ.get('HOSTNAME', '<noname>')
    date = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    event_type = 'Warning' if err else 'Normal'
    reason = 'Failed' if err else 'Created'
    event_id = int(round(time.time() * 1000))
    source_component = 'volume-backup'
    event_namespace = getattr(claim_ref, 'namespace', os.environ.get('NAMESPACE', 'default'))

    print(f'Generating event: err={err} pv={pv.metadata.name if pv else "<unknown>"} namespace={event_namespace} pvc={claim_ref.name if claim_ref else ""}')

    event = {
        'apiVersion': 'v1',
        'kind': 'Event',
        'metadata': {
            'name': f'{source_component}.{event_id}',
            'namespace': event_namespace
        },
        'type': event_type,
        'count': 1,
        'message': err,
        'firstTimestamp': date,
        'lastTimestamp': date,
        'reason': reason,
        'involvedObject': {
            'kind': 'PersistentVolumeBackup',
            'namespace': event_namespace,
            'name': pv.metadata.name if pv else '<unknown>',
        },
        'reportingComponent': 'backup.getup.io/database',
        'reportingInstance': hostname,
        'source': {
            'component': source_component,
            'host': source_component ## TODO: discover node hostname
        }
    }

    return corev1.create_namespaced_event(body=event, namespace=event_namespace)


def pv_is_bound(pv):
    try:
        return pv.status.phase == 'Bound'
    except:
        pass


def pv_provisioner(pv):
    try:
        ann = pv.metadata.annotations
        return ann.get('pv.kubernetes.io/provisioned-by') or ann.get('volume.beta.kubernetes.io/storage-provisioner')
    except:
        pass


def exclude_pv(pv):
    try:
        return pv.metadata.annotations.get('backup.getup.io/ignore-snapshot', False)
    except:
        pass


def pv_is_valid(pv):
    return ((exclude_pv(pv) is False)
            and pv_is_bound(pv)
            and (pv_provisioner(pv) in ('kubernetes.io/aws-ebs', 'kubernetes.io/gce-pd')))


def list_pvs(corev1):
    print(f'--> Listing persistent volumes')
    pvs = list(filter(pv_is_valid, corev1.list_persistent_volume().items))
    print(f'--> Found {len(pvs)} persistent volume(s)')
    return pvs


def get_claim_ref(pv):
    if pv_is_bound(pv) and hasattr(pv.spec, 'claim_ref'):
        return pv.spec.claim_ref


try:
    if args.create_snapshots:
        for pv in list_pvs(corev1):
            try:
                ret = provider.create_snapshot(pv, dry_run=args.dry_run)

                if ret:
                    gen_event(corev1, ret['pv'])
            except Exception as ex:
                gen_event(corev1, pv=None, err=str(ex))

    if args.clean_old_snapshots:
        clean_before = datetime.utcnow() - relativedelta(days=args.retention_days)
        clean_before = clean_before.replace(tzinfo=timezone.utc)

        print(f'--> Cleaning snapshots older than {args.retention_days} days, before {clean_before}')

        for snapshot in provider.list_snapshots():
            if provider.expired_snapshot(snapshot, clean_before):
                provider.delete_snapshot(snapshot, dry_run=args.dry_run)
except Exception as ex:
    gen_event(corev1, pv=None, err=str(ex))
