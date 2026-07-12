import type { ClothingItem } from '../types';

export const clothingItems: { value: ClothingItem; label: string; description: string }[] = [
  { value: 'schuhe', label: 'Schuhe', description: 'Shoes and sneakers' },
  { value: 'hosen', label: 'Hosen', description: 'Trousers, jeans, cargos, shorts' },
  { value: 'obenrum_warm', label: 'Obenrum Warm', description: 'T-shirts and short-sleeve shirts' },
  { value: 'obenrum_mittel', label: 'Obenrum Mittel', description: 'Longsleeves and light pullovers' },
  { value: 'obenrum_kalt', label: 'Obenrum Kalt', description: 'Heavy pullovers and jackets' },
  { value: 'kopf', label: 'Kopf', description: 'Funny or weird baseball caps' },
];

export function clothingItemLabel(value: ClothingItem): string {
  return clothingItems.find((item) => item.value === value)?.label ?? value;
}
