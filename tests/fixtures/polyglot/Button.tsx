export const Button = ({ label }: { label: string }) => {
  return <button onClick={() => track(label)}>{label}</button>;
};

function track(label: string): void {}
