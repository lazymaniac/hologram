import React, { memo, useEffect, useState } from 'react';

export interface Props {
  items: string[];
  onPick: (s: string) => void;
}

export function UserList({ items, onPick }: Props) {
  const [open, setOpen] = useState(false);
  useEffect(() => { load(); }, []);
  return (
    <section>
      {items.map((i) => (
        <UserCard key={i} onPick={onPick} />
      ))}
      <Badge count={items.length} />
    </section>
  );
}

export const UserCard: React.FC<Props> = ({ items }) => <li><Badge /></li>;

export const Badge: React.FC<Props> = ({ items }) => <em>{items.length}</em>;

function load(): void {}

export default memo(function Page() {
  return <UserList items={[]} onPick={pick} />;
});

function pick(s: string): void {}
