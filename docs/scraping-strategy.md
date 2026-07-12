# Vinted scraping strategy

## Principles

- start with one Vinted region
- keep session handling region-scoped
- prefer manual cookie/session input in the first release
- test parsers against saved fixtures before relying on live traffic
- separate session/bootstrap from listing parsing logic

## Adapter layers

1. **Session layer**
   - validate presence of cookie/session material
   - store freshness metadata
   - record last success and last failure reason

2. **Search execution layer**
   - translate saved searches + filters into Vinted requests
   - normalize raw responses into candidate entities
   - track dedupe keys and rate-limit state

3. **Enrichment layer**
   - download candidate images
   - extract structured fashion features
   - compute transparent score traces

## Anti-fragility notes

- do not assume one stable HTTP endpoint forever
- keep request/response fixtures under version control for parser contract tests
- add backoff + observability for blocks, throttling, and invalid sessions
- delay any auto-login/browser automation until manual-cookie flows are stable

## Storage notes

- persist search state, candidate history, feedback, and learning snapshots in SQLite
- persist uploaded wardrobe images and any cached listing media in mounted local storage
- keep the runtime deployable through Docker Compose without external infrastructure dependencies
