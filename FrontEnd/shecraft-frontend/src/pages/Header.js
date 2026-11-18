
import React from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import "../styles/header.css";

import logoImg from "../assets/logo.png";
import addToCartImg from "../assets/addtocart.png";
import personImg from "../assets/person.png";
import orderImg from "../assets/order.png"; 
import useAuth from "../context/AuthContext";

const Header = ({ openLogin }) => {
  const navigate = useNavigate();
  const { user } = useAuth();
  const isLoggedIn = !!user;

  const handleProtectedNav = (path) => {
    if (!isLoggedIn) openLogin?.();
    else navigate(path);
  };

  return (
    <header className="sc-header">
      <div className="sc-left">
        {/* LOGO */}
        <Link to="/" className="logo-button" aria-label="SheCraft Home">
          <img src={logoImg} alt="SheCraft logo" className="logo-img" />
        </Link>

        {/* CART */}
        <button
          type="button"
          className="icon-button"
          aria-label="Cart"
          onClick={() => handleProtectedNav("/cart")}
        >
          <img src={addToCartImg} alt="Cart" className="icon-img" />
        </button>

        {/* ORDERS */}
        {/* ORDERS */}
<button
  type="button"
  className="icon-button"
  aria-label="Orders"
  onClick={() => {
    if (!isLoggedIn) return openLogin?.();
    const target = user?.role === "designer" ? "/designer/dashboard" : "/orderpage";
    navigate(target);
  }}
>
  <img src={orderImg} alt="Orders" className="icon-img" />
</button>


        {/* ACCOUNT */}
        <button
          type="button"
          className="icon-button"
          aria-label="Account"
          onClick={() => handleProtectedNav("/account")}
        >
          <img src={personImg} alt="Account" className="icon-img" />
        </button>
      </div>

      {/* NAV LINKS */}
      <nav className="sc-nav-links" aria-label="Main navigation">
        <NavLink to="/" end>Home</NavLink>
        <NavLink to="/about">About Us</NavLink>

        {!isLoggedIn ? (
          <button type="button" className="sc-header-login" aria-haspopup="dialog" onClick={openLogin}>Log in</button>
        ) : (
          <NavLink to="/account">My Account</NavLink>
        )}
      </nav>
    </header>
  );
};

export default Header;

