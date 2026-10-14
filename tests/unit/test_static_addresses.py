import pytest

from lab_control.application.static_addresses import StaticAddressStore
from lab_control.discovery.lan import StaticAddressDiscoveryProvider
from lab_control.domain.instruments import ValidationError
from lab_control.storage.repository import Repository


def test_static_addresses_round_trip_and_provider_uses_exact_socket(tmp_path):
    repository = Repository(tmp_path)
    try:
        store = StaticAddressStore(repository)
        entry = store.add("192.168.0.120", label="实验室光谱仪")
        assert store.entries() == (entry,)

        candidate = StaticAddressDiscoveryProvider(store).enumerate()[0]
        assert candidate.config.resource == "TCPIP0::192.168.0.120::10001::SOCKET"
        assert candidate.config.adapter == "ceyear.6362d"
        assert candidate.descriptor["static_address_id"] == entry.id
        assert not candidate.safe_scpi

        reloaded = StaticAddressStore(repository).entries()
        assert reloaded[0].label == "实验室光谱仪"
        assert store.remove({entry.id}) == 1
        assert store.entries() == ()
    finally:
        repository.close()


@pytest.mark.parametrize("value", ["", "192.168.0.999", "127.0.0.1", "224.0.0.1"])
def test_static_address_validation_rejects_non_device_ipv4(value, tmp_path):
    repository = Repository(tmp_path)
    try:
        with pytest.raises(ValidationError):
            StaticAddressStore(repository).add(value)
    finally:
        repository.close()


def test_static_address_duplicate_resource_is_rejected(tmp_path):
    repository = Repository(tmp_path)
    try:
        store = StaticAddressStore(repository)
        store.add("192.168.0.120")
        with pytest.raises(ValidationError, match="已在列表"):
            store.add("192.168.0.120", adapter="")
    finally:
        repository.close()


def test_serial_endpoint_uses_serial_transport_and_can_share_the_same_dialog_store(tmp_path):
    repository = Repository(tmp_path)
    try:
        store = StaticAddressStore(repository)
        entry = store.add("COM7", adapter="ceyear.6362d", kind="serial")
        candidate = StaticAddressDiscoveryProvider(store).enumerate()[0]
        assert entry.resource == "COM7"
        assert candidate.config.resource == "COM7"
        assert candidate.config.transport == "serial"
        assert candidate.config.adapter == "ceyear.6362d"
        assert candidate.config.baud_rate == 9600
    finally:
        repository.close()


def test_ip_endpoint_can_pin_a_local_source_address_without_changing_target_resource(tmp_path):
    repository = Repository(tmp_path)
    try:
        store = StaticAddressStore(repository)
        entry = store.add("192.168.5.241", source_address="192.168.0.1")
        config = entry.config()
        assert config.resource == "TCPIP0::192.168.5.241::10001::SOCKET"
        assert config.transport == "tcp"
        assert config.source_address == "192.168.0.1"
        reloaded = StaticAddressStore(repository).entries()[0]
        assert reloaded.source_address == "192.168.0.1"
    finally:
        repository.close()
