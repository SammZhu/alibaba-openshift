# Additively merge an Ignition fragment (.[1]) into a base config (.[0]):
# preserve ALL of the base (the agent ISO's own embedded ignition) and only
# concatenate storage.files + systemd.units. Used by tasks/iso_agent.yml on the
# mirror ECS to inject the clone-vdb-to-vda hook (jq -s -f this-file base add).
.[0] as $b | .[1] as $a
| $b
| .storage = (.storage // {})
| .storage.files = ((.storage.files // []) + ($a.storage.files // []))
| .systemd = (.systemd // {})
| .systemd.units = ((.systemd.units // []) + ($a.systemd.units // []))
