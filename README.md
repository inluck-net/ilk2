# ilk2

ilk2 is a small execution core that runs multi-step coding batches unattended on a Mac. It is the second version of the ilk loop engine in [`inluck-net/ilk-skills`](https://github.com/inluck-net/ilk-skills) (V1).

**Status:** design closed for P1, Oct 13, 2026 (tag `design-accepted-p1`). No code yet. Start with [`docs/README.md`](docs/README.md).

In one sentence: one supervisor process owns every run, a driver per run owns gates, commits and step progress, all state lives in one transactional store, and the worker model writes code only.
