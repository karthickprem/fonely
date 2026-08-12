"""Tests for tenant admission control."""
import threading

from fonely.voice.admission import AdmissionController


def test_admit_within_capacity():
    ac = AdmissionController(max_per_tenant=3, max_global=10)
    d = ac.try_admit("t1")
    assert d.admitted
    assert d.tenant_active == 1
    assert d.global_active == 1


def test_tenant_capacity_exceeded():
    ac = AdmissionController(max_per_tenant=2, max_global=10)
    ac.try_admit("t1")
    ac.try_admit("t1")
    d = ac.try_admit("t1")
    assert not d.admitted
    assert d.reason == "tenant_capacity"


def test_global_capacity_exceeded():
    ac = AdmissionController(max_per_tenant=10, max_global=2)
    ac.try_admit("t1")
    ac.try_admit("t2")
    d = ac.try_admit("t3")
    assert not d.admitted
    assert d.reason == "global_capacity"


def test_release_allows_readmission():
    ac = AdmissionController(max_per_tenant=1)
    ac.try_admit("t1")
    assert not ac.try_admit("t1").admitted
    ac.release("t1")
    assert ac.try_admit("t1").admitted


def test_concurrent_admission():
    ac = AdmissionController(max_per_tenant=100, max_global=50)
    results = []

    def worker(tid):
        for _ in range(10):
            results.append(ac.try_admit(tid))

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    admitted = sum(1 for r in results if r.admitted)
    assert admitted == 50  # global cap
    assert ac.stats()["total_admitted"] == 50
    assert ac.stats()["total_rejected"] == 50


def test_stats():
    ac = AdmissionController(max_per_tenant=5, max_global=20)
    ac.try_admit("t1")
    ac.try_admit("t2")
    s = ac.stats()
    assert s["global_active"] == 2
    assert s["tenants_active"] == 2
    assert s["total_admitted"] == 2
