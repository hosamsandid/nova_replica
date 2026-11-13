# Link Summary

## Counts

| Metric | Value |
|---|---|
| Files | 1510 |
| File edges | 6914 |
| Cross-component edges | 4673 |
| Resolved | 18824 |
| Ambiguous | 0 |
| Unresolved | 0 |
| External imports | 4061 |
| Seams | 92 |

## Component Dependencies

213 component pairs, 4673 cross-component file edges, 13297 resolved references.

| From | To | File Edges | Resolved Refs |
|---|---|---|---|
| accelerator | nova | 4 | 10 |
| accelerator | objects | 1 | 2 |
| accelerator | scheduler | 1 | 2 |
| api | compute | 53 | 101 |
| api | conductor | 1 | 2 |
| api | conf | 21 | 21 |
| api | image | 4 | 8 |
| api | limit | 3 | 6 |
| api | network | 10 | 34 |
| api | notifications | 1 | 2 |
| api | nova | 151 | 277 |
| api | objects | 34 | 108 |
| api | pci | 1 | 2 |
| api | policies | 57 | 57 |
| api | scheduler | 1 | 2 |
| api | servicegroup | 3 | 6 |
| api | share | 1 | 2 |
| api | virt | 3 | 8 |
| api | volume | 3 | 6 |
| cmd | compute | 5 | 9 |
| cmd | conductor | 2 | 3 |
| cmd | conf | 16 | 21 |
| cmd | console | 2 | 4 |
| cmd | db | 8 | 15 |
| cmd | limit | 2 | 2 |
| cmd | network | 2 | 5 |
| cmd | nova | 47 | 119 |
| cmd | objects | 18 | 66 |
| cmd | policies | 1 | 2 |
| cmd | scheduler | 3 | 7 |
| cmd | virt | 2 | 6 |
| cmd | volume | 2 | 4 |
| compute | accelerator | 3 | 7 |
| compute | conductor | 2 | 4 |
| compute | conf | 12 | 14 |
| compute | db | 5 | 13 |
| compute | image | 2 | 4 |
| compute | limit | 3 | 7 |
| compute | network | 6 | 15 |
| compute | notifications | 14 | 60 |
| compute | nova | 58 | 320 |
| compute | objects | 39 | 211 |
| compute | pci | 7 | 13 |
| compute | policies | 2 | 2 |
| compute | scheduler | 7 | 16 |
| compute | servicegroup | 1 | 2 |
| compute | share | 1 | 2 |
| compute | virt | 12 | 45 |
| compute | volume | 2 | 4 |
| conductor | accelerator | 1 | 3 |
| conductor | compute | 13 | 35 |
| conductor | conf | 4 | 4 |
| conductor | image | 3 | 6 |
| conductor | limit | 2 | 4 |
| conductor | network | 4 | 7 |
| conductor | notifications | 1 | 2 |
| conductor | nova | 24 | 67 |
| conductor | objects | 9 | 68 |
| conductor | scheduler | 7 | 27 |
| conductor | servicegroup | 1 | 2 |
| conductor | volume | 2 | 4 |
| conf | virt | 1 | 1 |
| console | compute | 1 | 2 |
| console | conf | 2 | 2 |
| console | nova | 11 | 27 |
| console | objects | 1 | 3 |
| db | compute | 2 | 2 |
| db | conf | 2 | 2 |
| db | nova | 10 | 58 |
| db | objects | 3 | 4 |
| image | conf | 1 | 3 |
| image | nova | 4 | 22 |
| image | objects | 2 | 4 |
| image | storage | 1 | 2 |
| keymgr | conf | 1 | 1 |
| keymgr | nova | 2 | 5 |
| limit | conf | 3 | 3 |
| limit | nova | 4 | 9 |
| limit | objects | 2 | 6 |
| limit | scheduler | 2 | 4 |
| network | accelerator | 1 | 4 |
| network | compute | 1 | 2 |
| network | conf | 1 | 1 |
| network | nova | 14 | 85 |
| network | objects | 3 | 20 |
| network | pci | 3 | 6 |
| network | policies | 1 | 1 |
| notifications | conf | 2 | 2 |
| notifications | image | 1 | 2 |
| notifications | nova | 6 | 10 |
| notifications | objects | 32 | 125 |
| nova | api | 1 | 3 |
| nova | compute | 2 | 3 |
| nova | conductor | 1 | 2 |
| nova | conf | 16 | 16 |
| nova | db | 8 | 13 |
| nova | limit | 2 | 7 |
| nova | notifications | 2 | 6 |
| nova | objects | 12 | 37 |
| nova | pci | 1 | 2 |
| nova | policies | 1 | 2 |
| nova | scheduler | 3 | 5 |
| nova | servicegroup | 1 | 2 |
| nova | tests-fixtures | 1 | 26 |
| nova | tests-unit | 1 | 2 |
| nova | virt | 5 | 11 |
| objects | compute | 6 | 14 |
| objects | conductor | 1 | 2 |
| objects | conf | 6 | 6 |
| objects | db | 56 | 248 |
| objects | network | 3 | 6 |
| objects | notifications | 5 | 12 |
| objects | nova | 51 | 170 |
| objects | scheduler | 1 | 2 |
| objects | share | 1 | 2 |
| objects | virt | 4 | 7 |
| pci | conf | 2 | 2 |
| pci | network | 1 | 1 |
| pci | nova | 11 | 27 |
| pci | objects | 9 | 17 |
| privsep | conf | 1 | 1 |
| privsep | nova | 3 | 7 |
| scheduler | compute | 4 | 12 |
| scheduler | conf | 23 | 23 |
| scheduler | network | 1 | 2 |
| scheduler | nova | 29 | 91 |
| scheduler | objects | 17 | 57 |
| scheduler | pci | 2 | 4 |
| scheduler | servicegroup | 2 | 4 |
| scheduler | virt | 4 | 25 |
| servicegroup | conf | 3 | 3 |
| servicegroup | nova | 4 | 7 |
| share | conf | 1 | 1 |
| share | nova | 2 | 10 |
| storage | conf | 1 | 1 |
| storage | nova | 3 | 10 |
| tests-fixtures | api | 2 | 4 |
| tests-fixtures | compute | 2 | 4 |
| tests-fixtures | conf | 7 | 11 |
| tests-fixtures | db | 3 | 7 |
| tests-fixtures | network | 2 | 3 |
| tests-fixtures | nova | 17 | 49 |
| tests-fixtures | objects | 6 | 9 |
| tests-fixtures | policies | 1 | 2 |
| tests-fixtures | privsep | 1 | 1 |
| tests-fixtures | scheduler | 1 | 2 |
| tests-fixtures | tests-functional | 1 | 2 |
| tests-fixtures | tests-unit | 1 | 1 |
| tests-fixtures | virt | 11 | 42 |
| tests-functional | api | 5 | 8 |
| tests-functional | cmd | 3 | 9 |
| tests-functional | compute | 56 | 67 |
| tests-functional | conductor | 2 | 2 |
| tests-functional | conf | 31 | 34 |
| tests-functional | db | 24 | 73 |
| tests-functional | image | 2 | 3 |
| tests-functional | limit | 2 | 2 |
| tests-functional | network | 12 | 17 |
| tests-functional | nova | 301 | 621 |
| tests-functional | objects | 122 | 423 |
| tests-functional | pci | 1 | 2 |
| tests-functional | policies | 13 | 13 |
| tests-functional | scheduler | 10 | 17 |
| tests-functional | tests | 1 | 2 |
| tests-functional | tests-fixtures | 175 | 623 |
| tests-functional | tests-unit | 41 | 78 |
| tests-functional | virt | 39 | 57 |
| tests-functional | volume | 3 | 4 |
| tests-unit | accelerator | 3 | 8 |
| tests-unit | api | 204 | 429 |
| tests-unit | cmd | 7 | 30 |
| tests-unit | compute | 199 | 319 |
| tests-unit | conductor | 12 | 33 |
| tests-unit | conf | 58 | 67 |
| tests-unit | console | 18 | 37 |
| tests-unit | db | 70 | 300 |
| tests-unit | hacking | 1 | 14 |
| tests-unit | image | 11 | 34 |
| tests-unit | keymgr | 1 | 2 |
| tests-unit | limit | 16 | 29 |
| tests-unit | network | 50 | 176 |
| tests-unit | notifications | 10 | 30 |
| tests-unit | nova | 787 | 2236 |
| tests-unit | objects | 362 | 1753 |
| tests-unit | pci | 25 | 59 |
| tests-unit | policies | 70 | 71 |
| tests-unit | privsep | 11 | 69 |
| tests-unit | scheduler | 83 | 206 |
| tests-unit | servicegroup | 8 | 13 |
| tests-unit | share | 1 | 3 |
| tests-unit | storage | 5 | 9 |
| tests-unit | tests | 1 | 3 |
| tests-unit | tests-fixtures | 85 | 246 |
| tests-unit | virt | 209 | 795 |
| tests-unit | volume | 12 | 18 |
| virt | api | 5 | 10 |
| virt | compute | 31 | 47 |
| virt | conf | 49 | 62 |
| virt | console | 5 | 18 |
| virt | image | 5 | 10 |
| virt | network | 10 | 17 |
| virt | nova | 122 | 468 |
| virt | objects | 36 | 108 |
| virt | pci | 7 | 20 |
| virt | privsep | 26 | 96 |
| virt | scheduler | 2 | 4 |
| virt | servicegroup | 1 | 2 |
| virt | storage | 3 | 6 |
| virt | volume | 1 | 2 |
| volume | conf | 1 | 1 |
| volume | nova | 4 | 21 |
| wsgi | api | 2 | 4 |
| wsgi | nova | 2 | 4 |

## Seams

| Kind | Count |
|---|---|
| http-endpoint | 5 |
| bus-topic | 1 |
| db-table | 84 |
| env-var | 2 |
| executable | 0 |
| native-symbol | 0 |

| Component A | Component B | Kind | Literal |
|---|---|---|---|
| api | db | db-table | a |
| api | db | db-table | metadata |
| api | db | db-table | tags |
| api | nova | db-table | metadata |
| api | pci | db-table | a |
| api | policies | db-table | a |
| api | policies | db-table | metadata |
| api | scheduler | db-table | a |
| api | tests-functional | db-table | a |
| api | tests-unit | db-table | a |
| api | tests-unit | http-endpoint | /compute/v1.1 |
| api | tests-unit | http-endpoint | /v1.1 |
| api | virt | db-table | a |
| cmd | compute | db-table | for |
| cmd | compute | db-table | instance |
| cmd | compute | db-table | of |
| cmd | conductor | db-table | instance |
| cmd | network | db-table | instance |
| cmd | nova | db-table | allocations |
| cmd | scheduler | db-table | allocations |
| cmd | tests-functional | db-table | allocations |
| cmd | tests-unit | db-table | allocations |
| cmd | virt | db-table | instance |
| compute | conductor | db-table | instance |
| compute | db | db-table | an |
| compute | network | db-table | an |
| compute | network | db-table | instance |
| compute | nova | db-table | an |
| compute | policies | db-table | an |
| compute | tests-unit | db-table | network |
| compute | tests-unit | db-table | source |
| compute | tests-unit | db-table | target |
| compute | virt | db-table | instance |
| conductor | network | db-table | instance |
| conductor | virt | db-table | instance |
| conf | db | db-table | it |
| db | network | db-table | an |
| db | nova | db-table | an |
| db | nova | db-table | metadata |
| db | pci | db-table | a |
| db | policies | db-table | a |
| db | policies | db-table | an |
| db | policies | db-table | metadata |
| db | policies | db-table | quotas |
| db | scheduler | db-table | a |
| db | scheduler | db-table | aggregates |
| db | tests-functional | db-table | a |
| db | tests-unit | db-table | a |
| db | tests-unit | db-table | cell_mappings |
| db | tests-unit | db-table | flavors |
| db | virt | db-table | a |
| image | tests-unit | http-endpoint | /v1/images/{} |
| network | nova | db-table | an |
| network | nova | db-table | ports |
| network | policies | db-table | an |
| network | tests-functional | db-table | ports |
| network | tests-unit | db-table | port |
| network | virt | db-table | instance |
| nova | policies | db-table | an |
| nova | policies | db-table | metadata |
| nova | scheduler | db-table | allocations |
| nova | tests-functional | db-table | allocations |
| nova | tests-functional | db-table | neutron |
| nova | tests-functional | db-table | ports |
| nova | tests-unit | db-table | allocations |
| nova | tests-unit | db-table | machine |
| nova | tests-unit | env-var | OS_NOVA_DISABLE_EVENTLET_PATCHING |
| nova | virt | db-table | machine |
| pci | policies | db-table | a |
| pci | scheduler | db-table | a |
| pci | tests-functional | db-table | a |
| pci | tests-unit | db-table | a |
| pci | virt | db-table | a |
| policies | scheduler | db-table | a |
| policies | tests-functional | db-table | a |
| policies | tests-unit | db-table | a |
| policies | virt | db-table | a |
| scheduler | tests-functional | db-table | a |
| scheduler | tests-functional | db-table | allocations |
| scheduler | tests-unit | db-table | a |
| scheduler | tests-unit | db-table | allocations |
| scheduler | tests-unit | db-table | host |
| scheduler | virt | db-table | a |
| tests-fixtures | tests-functional | env-var | OS_DEBUG |
| tests-functional | tests-unit | bus-topic | compute |
| tests-functional | tests-unit | db-table | a |
| tests-functional | tests-unit | db-table | allocations |
| tests-functional | tests-unit | http-endpoint | /v2 |
| tests-functional | virt | db-table | a |
| tests-unit | virt | db-table | a |
| tests-unit | virt | db-table | machine |
| tests-unit | virt | http-endpoint | /xmlns/libvirt/nova/1.1 |

Kinds that produced no seams: executable, native-symbol.

## Unresolved Imports

None.

## Ambiguous References

None.

## Warnings

None.
