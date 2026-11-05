# mlx5 Verbs/DevX queue-size backport

mlx5-verbs-queue-limits.patch adapts Viacheslav Ovsiienko's upstream DPDK patch,
"net/mlx5: fix the maximal queue size query":
https://mails.dpdk.org/archives/stable/2025-May/052505.html

The upstream patch repairs limits when the mlx5 driver uses Verbs rather than
DevX: it queries the appropriate capability instead of using an uninitialized
DevX field and reporting a maximum of one descriptor. It also updates queue
validation and sizing consistently in seven source files.

Adaptations for this checkout:

- Preserve the pinned revision's error-return handling for txq_adjust_params().
- Keep replaced upstream lines as comments for source comparison.

The patch was application-tested against DPDK commit
fa3aca8a525decfd12bd5fa01ebc98789d5f7278, which is the repository's gitlink.
The CloudLab build adds its existing Caladan patches on top of that revision.
fix-mlx5.sh checks patch applicability on the actual deployed tree before applying
anything, and accepts an already-applied patch without duplicating it. The local
check is not a full DPDK build or proof of hardware compatibility.

Run ../fix-mlx5.sh after dependencies and Caladan are built. It applies this patch,
rebuilds and installs DPDK into its existing build prefix, then forces IOKernel
relinking against the new static library. It does not rerun submodule initialization,
create commits, change firmware, or load a different kernel driver.
