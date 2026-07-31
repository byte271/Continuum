# Roadmap

Each milestone is release-gated. A code path is not complete until its
semantics, rejection boundaries, tests, measurements, and relevant native
platform evidence are retained.

Completed milestones stay listed with their evidence so a later regression is
visible as a change, not as a silently dropped line.

1. **Done.** Publish the clean IR 0.3 revision, now runtime 0.2.0, with all
   145 tests and the 50-program corpus reports.
2. **Done.** Generate a new IR 0.3 image on the native Linux x86_64 Actions job
   and rerun the dependent native Apple Silicon macOS arm64 proof without
   reusing any IR 0.2 image. Passed in Actions run 30509186641 at commit
   `3a4a43fb74331113225d7b9a3a0fef4afd1371fa`.
3. **Done.** Run `runtime-bundles.yml` on every native platform, now Linux
   x86_64, macOS arm64, and Windows x86_64. The moved archive, full suite,
   transactional installer, and `continuum doctor` pass on each. Publishing a
   download additionally requires milestone 6.
4. Add a Windows leg to the cross-platform proof: a `source_windows.py` or
   `target_windows.py` under `validation/cross_platform/`, a dependent job in
   `cross-platform-proof.yml`, and the same evidence-transfer and condition
   requirements the existing pair uses. Until this lands, no cross-platform
   claim involving Windows may appear anywhere in this repository, and
   `continuum doctor` must keep reporting the Windows pair as
   format-compatible only.
5. Publish the immutable `v0.1.0-proof` release at commit
   `15bceefece050d06a1f504244a77434e31fd5228`, including the exact proof image,
   Linux and final evidence archives, verification summary, and SHA-256
   manifest.
6. **Partly done.** Versioned Linux x86_64, macOS arm64, and Windows x86_64
   bundles are published at immutable v0.3.0 release URLs with SHA-256
   sidecars, and the PowerShell installer was exercised from its published URL
   through to a working continuation. Still outstanding: the same network
   install for the Linux and macOS archives.
7. Run the unchanged 50-program corpus in the native cross-platform workflow
   and retain per-program Linux-image/macOS-resume results. Regenerate the
   corpus report natively on macOS arm64 and Windows x86_64 so the published
   compatibility rate stops being a Linux-only number.
8. Measure Windows idle safe-point and freeze latency with `benchmarks/`, so
   the 10 ms request-file poll has a published cost next to the POSIX
   signal path in `PERFORMANCE.md`.
9. Implement portable dictionary-view iteration or explicitly narrow it after
   differential mutation tests establish the required semantics. Regenerate
   the corpus and require a new-process checkpoint test.
10. Add structure-aware image fuzzing for graph references, frame metadata,
    limits, ZIP entry metadata, and capability negotiation. Every discovered
    crash becomes a minimized rejection regression.
11. Profile local-name dictionary access on the current workload. Introduce
    compact local slots only if the profile identifies it as the largest
    remaining runtime cost and the image representation stays portable.
12. Add a second unchanged application demonstration selected from the corpus;
    require nested active frames, a nontrivial graph, an external anti-restart
    audit, and an uninterrupted-control comparison.
