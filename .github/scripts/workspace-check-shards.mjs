// Split the workspace-check list across n jobs: shard i/n keeps every n-th unit starting at the
// i-th, so the n shards together run the full list exactly once.

/**
 * @template T
 * @param {T[]} units
 * @param {string} spec `i/n` with 1 <= i <= n
 * @returns {T[]}
 */
export function shardUnits(units, spec) {
  const match = /^([1-9]\d*)\/([1-9]\d*)$/.exec(String(spec))
  const [index, total] = match ? [Number(match[1]), Number(match[2])] : [0, 0]
  if (!(match && index <= total)) {
    throw new RangeError(`--shard takes i/n with 1 <= i <= n, got ${spec}`)
  }
  return units.filter((_, k) => k % total === index - 1)
}
