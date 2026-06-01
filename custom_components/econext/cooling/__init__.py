"""UFH cooling controller feature for the econext integration.

A self-contained dew-safe cooling controller that drives a configurable
heating circuit's cooling target through the econext coordinator. The
state machine (`state_machine.tick`), safety policy (`safety`), schedule
flap detector (`schedule`) and dataclasses (`models`) are pure and
HA-agnostic; `driver.CoolingDriver` wires them to the coordinator.
"""
