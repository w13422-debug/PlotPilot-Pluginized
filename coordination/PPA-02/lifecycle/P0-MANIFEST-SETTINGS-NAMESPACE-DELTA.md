# P0 Delta — manifest Settings namespace cross-field binding

## Identity

- Node: `NW-P2-LIFECYCLE-02`
- Remediation parent: `dacf35dab01a9be325c09cfab002ac887684b16d`
- Owner required: P0 public contracts / Plugin SDK verifier
- P2 status: **public verifier/golden slice stopped**

## Gap

The published manifest schema constrains `settings.namespace` only by pattern;
it cannot express the required cross-field equality
`settings.namespace == "plugin." + plugin_id`.  The published SDK verifier also
does not enforce that equality.  P2 is not authorized to change either path.

## Requested P0 change

Add the equality check to the authoritative manifest verifier and add negative
contract/golden corpus for a valid plugin ID paired with another plugin's
namespace.  The public schema may retain its pattern, but must not be presented
as sufficient cross-field validation.

## Owned fail-closed boundary

`InstallStager` and `InstallLifecycleService.begin` now perform the equality
check after package verification and before creating an Attempt, staging bytes,
publishing a package or registering retirement state.  This protects the P2
install path only; it is not a substitute for the P0 public verifier change.
