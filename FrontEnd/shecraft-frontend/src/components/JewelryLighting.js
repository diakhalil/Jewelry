import { Environment, Lightformer } from "@react-three/drei";

export default function JewelryLighting() {
  return (
    <Environment resolution={128}>
      <Lightformer intensity={4} scale={[5, 2, 1]} position={[0, 4, 1]} />
      <Lightformer intensity={3} scale={[2, 5, 1]} position={[4, 0, 0]} />
      <Lightformer intensity={2} scale={[4, 3, 1]} position={[-3, 1, -3]} />
    </Environment>
  );
}
