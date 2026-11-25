import { Component, Suspense, useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { Canvas } from "@react-three/fiber";
import { Center, Html, OrbitControls, useGLTF } from "@react-three/drei";
import { ArrowLeft, RotateCcw } from "lucide-react";
import { Box3, Vector3 } from "three";
import JewelryLighting from "../components/JewelryLighting";
import { jewelryPreviewUrl } from "../api/chat";
import "../styles/sapphirepreview.css";

const demoUrl = `${process.env.PUBLIC_URL}/models/gold-sapphire-ring.glb`;

function JewelryModel({ modelUrl }) {
  const { scene } = useGLTF(modelUrl);
  const scale = useMemo(() => {
    const { x, y, z } = new Box3().setFromObject(scene).getSize(new Vector3());
    return 2 / Math.max(x, y, z);
  }, [scene]);

  return (
    <group scale={scale}>
      <Center>
        <primitive object={scene} />
      </Center>
    </group>
  );
}

class PreviewErrorBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <p className="sapphire-preview-status" role="alert">{this.props.errorMessage}</p>;
    }
    return this.props.children;
  }
}

export default function SapphireRingPreview() {
  const controls = useRef(null);
  const [autoRotate, setAutoRotate] = useState(false);
  const { id } = useParams();
  const { state } = useLocation();
  const modelUrl = id ? jewelryPreviewUrl(id) : demoUrl;
  const title = id ? state?.title || "Your 3D jewelry design" : "Gold & Sapphire Ring";
  const errorMessage = id
    ? "This preview could not be loaded. Open Ask SheCraft and generate it again."
    : "The 3D ring could not be loaded.";

  return (
    <section className="sapphire-preview" aria-labelledby="sapphire-preview-title">
      <div className="sapphire-preview-content">
        <Link to={id ? "/" : "/rings"} className="sapphire-preview-back">
          <ArrowLeft size={18} aria-hidden="true" />
          {id ? "Back to collections" : "Back to rings"}
        </Link>
        <h1 id="sapphire-preview-title">{title}</h1>
        <p>Drag to rotate. Scroll or pinch to zoom.</p>

        <div className="sapphire-preview-stage" role="region" aria-label="Interactive 3D jewelry preview">
          <PreviewErrorBoundary key={modelUrl} errorMessage={errorMessage}>
            <Canvas
              camera={{ position: [2.25, 1.05, 3.8], fov: 37 }}
              dpr={[1, 2]}
              fallback={<p className="sapphire-preview-status" role="alert">3D viewing is unavailable in this browser.</p>}
            >
              <JewelryLighting />
              <hemisphereLight intensity={1} groundColor="#766c84" />
              <directionalLight intensity={2.5} position={[3, 5, 4]} />
              <Suspense fallback={<Html center><p className="sapphire-preview-status" role="status">Loading 3D preview…</p></Html>}>
                <JewelryModel modelUrl={modelUrl} />
              </Suspense>
              <OrbitControls
                ref={controls}
                autoRotate={autoRotate}
                autoRotateSpeed={1.5}
                enablePan={false}
                minDistance={2.75}
                maxDistance={7}
              />
            </Canvas>
          </PreviewErrorBoundary>
        </div>

        <div className="sapphire-preview-controls">
          <button type="button" className="sc-button" onClick={() => controls.current?.reset()}>
            <RotateCcw size={16} aria-hidden="true" />
            Reset view
          </button>
          <button
            type="button"
            className="sc-button"
            aria-pressed={autoRotate}
            onClick={() => setAutoRotate((previous) => !previous)}
          >
            Auto-rotate
          </button>
        </div>
      </div>
    </section>
  );
}
