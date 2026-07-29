# Roadmap

Each milestone is release-gated. A code path is not complete until its
semantics, rejection boundaries, tests, measurements, and relevant native
platform evidence are retained.

1. Publish the clean IR 0.3/runtime 0.2.0.dev0 revision with all 79 tests and
   the 50-program corpus reports.
2. Generate a new IR 0.3 image on the native Linux x86_64 Actions job and
   rerun the dependent native Apple Silicon macOS arm64 proof without reusing
   any IR 0.2 image or weakening the 26 conditions.
3. Run `runtime-bundles.yml` on both native platforms. Require the moved
   archive, full suite, transactional installer, and `continuum doctor` to
   pass before publishing either download.
4. Publish the immutable `v0.1.0-proof` release at commit
   `15bceefece050d06a1f504244a77434e31fd5228`, including the exact proof image,
   Linux and final evidence archives, verification summary, and SHA-256
   manifest.
5. Publish versioned Linux x86_64 and macOS arm64 self-contained runtime
   bundles only after milestone 3, then test the documented one-line installer
   from the immutable release URLs.
6. Run the unchanged 50-program corpus in the native cross-platform workflow
   and retain per-program Linux-image/macOS-resume results.
7. Implement portable dictionary-view iteration or explicitly narrow it after
   differential mutation tests establish the required semantics. Regenerate
   the corpus and require a new-process checkpoint test.
8. Add structure-aware image fuzzing for graph references, frame metadata,
   limits, ZIP entry metadata, and capability negotiation. Every discovered
   crash becomes a minimized rejection regression.
9. Profile local-name dictionary access on the current workload. Introduce
   compact local slots only if the profile identifies it as the largest
   remaining runtime cost and the image representation stays portable.
10. Add a second unchanged application demonstration selected from the corpus;
    require nested active frames, a nontrivial graph, an external anti-restart
    audit, and an uninterrupted-control comparison.
