import { Link } from "react-router-dom";
import "../styles/necklaceselections.css";

import nameNecklaceImg from "../assets/necklace/name.png";
import charmNecklaceImg from "../assets/necklace/charms.png";
import birthstoneNecklaceImg from "../assets/necklace/birthstone.png";


const necklaces = [
  {
    id: 1,
    type: "name",
    label: "Name Necklace",
    image: nameNecklaceImg,
    theme: "name-theme",
  },
  {
    id: 2,
    type: "charm",
    label: "Charm Necklace",
    image: charmNecklaceImg,
    theme: "charm-theme",
  },
  {
    id: 3,
    type: "birthstone",
    label: "Birthstone Necklace",
    image: birthstoneNecklaceImg,
    theme: "birthstone-theme",
  },
];

export default function NecklaceSelection() {
return (
  <div className="page-wrapper">
    <div className="necklace-selection-page">
      <h1 className="necklace-title">Choose Your Necklace</h1>
      <p className="necklace-subtitle">
        Personalize your necklace with your initials, charms, or birthstones.
      </p>

      <div className="necklaces-grid">
        {necklaces.map((necklace) => (
          <Link
            key={necklace.id}
            className={`necklace-card ${necklace.theme}`}
            to={necklace.type === "name" ? "/necklacespage" : `/necklaces/${necklace.type}`}
            state={necklace.type === "name" ? { necklaceType: necklace.type } : undefined}
          >
            <div className="necklace-image-container">
              <img src={necklace.image} alt="" />
            </div>
            <p className="necklace-label">{necklace.label}</p>
          </Link>
        ))}
      </div>
    </div>

    
  </div>
);
}
