import { expect, test } from 'vitest'
import { shardUnits } from '../.github/scripts/workspace-check-shards.mjs'

const units = Array.from({ length: 10 }, (_, k) => `pkg-${k} :: check`)

test('n shards partition the check list: every check runs exactly once', () => {
  for (const total of [1, 3, 8, 10, 12]) {
    const shards = Array.from({ length: total }, (_, i) => shardUnits(units, `${i + 1}/${total}`))
    expect(shards.flat().sort()).toEqual([...units].sort())
  }
})

test('a shard past the list length is empty, not an error', () => {
  expect(shardUnits(units.slice(0, 2), '3/8')).toEqual([])
})

test('a malformed or out-of-range spec is refused by name', () => {
  for (const spec of ['0/8', '9/8', '8', 'a/b', '1.5/8', undefined]) {
    expect(() => shardUnits(units, spec)).toThrow('--shard')
  }
})
