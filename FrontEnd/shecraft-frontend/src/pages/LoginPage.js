import { useEffect, useId, useRef, useState } from "react";
import { X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import useAuth from "../context/AuthContext";
import "../styles/login.css";

export default function LoginPage({ closePopup }) {
  const navigate = useNavigate();
  const { login } = useAuth();
  const dialogRef = useRef(null);
  const titleId = useId();
  const [activeTab, setActiveTab] = useState("login");
  const [role, setRole] = useState("customer");
  const [errorMessage, setErrorMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const signingUp = activeTab === "signup";
  const designer = role === "designer";

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog.open) dialog.showModal();
  }, []);

  function dismiss() {
    if (closePopup) closePopup();
    else navigate("/", { replace: true });
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (loading) return;
    const fields = new FormData(event.currentTarget);
    const email = fields.get("email").trim();
    const password = fields.get("password");
    const name = fields.get("name")?.trim();
    const branch = fields.get("branch")?.trim();

    if (signingUp && (!name || (designer && !branch))) {
      setErrorMessage(!name ? "Enter your name." : "Enter your branch.");
      return;
    }

    setErrorMessage("");
    setLoading(true);

    try {
      let data;
      if (!signingUp) {
        data = await api.login({ role, email, password });
      } else if (designer) {
        data = await api.signupDesigner({ name, branch, email, password });
      } else {
        data = await api.signupCustomer({ name, email, password });
      }

      if (!data.token || data.user?.role !== role || !data.user.id) {
        throw new Error("SheCraft could not confirm your account. Please try again.");
      }

      login({ role, user: data.user });
      closePopup?.();
      navigate(designer ? "/designer/dashboard" : "/orderpage", { replace: !closePopup });
    } catch (error) {
      setErrorMessage(error instanceof TypeError
        ? "Could not reach SheCraft. Please try again."
        : error.message || "Authentication failed. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <dialog
      className="sc-login"
      ref={dialogRef}
      aria-labelledby={titleId}
      onClose={dismiss}
      onCancel={(event) => { if (loading) event.preventDefault(); }}
      onClick={(event) => {
        if (!loading && event.target === event.currentTarget) event.currentTarget.close();
      }}
    >
      <div className="sc-login-content">
        <header className="sc-login-heading">
          <h1 id={titleId}>{signingUp ? "Create your account" : "Welcome to SheCraft"}</h1>
          <button
            type="button"
            className="sc-login-close"
            aria-label="Close login"
            disabled={loading}
            onClick={() => dialogRef.current.close()}
          >
            <X size={20} aria-hidden="true" />
          </button>
        </header>

        <div className="sc-login-tabs" role="group" aria-label="Account access">
          {[["login", "Log in"], ["signup", "Sign up"]].map(([tab, label]) => (
            <button
              key={tab}
              type="button"
              aria-pressed={activeTab === tab}
              disabled={loading}
              onClick={() => { setActiveTab(tab); setErrorMessage(""); }}
            >
              {label}
            </button>
          ))}
        </div>

        <form key={activeTab} onSubmit={handleSubmit} className="sc-login-form">
          <fieldset className="sc-login-roles" disabled={loading}>
            <legend>Account type</legend>
            <div>
              {[["customer", "Customer"], ["designer", "Designer"]].map(([value, label]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name="role"
                    value={value}
                    checked={role === value}
                    onChange={() => { setRole(value); setErrorMessage(""); }}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="sc-login-fields" disabled={loading}>
            {signingUp && (
              <label>
                Full name
                <input type="text" name="name" autoComplete="name" required />
              </label>
            )}
            {signingUp && designer && (
              <label>
                Branch
                <input type="text" name="branch" required />
              </label>
            )}
            <label>
              Email
              <input type="email" name="email" autoComplete={signingUp ? "email" : "username"} required />
            </label>
            <label>
              Password
              <input type="password" name="password" autoComplete={signingUp ? "new-password" : "current-password"} required />
            </label>
          </fieldset>

          {errorMessage && <p className="sc-login-error" role="alert">{errorMessage}</p>}
          <button className="sc-button sc-login-submit" type="submit" disabled={loading}>
            {loading ? (signingUp ? "Creating account…" : "Logging in…") : (signingUp ? "Create account" : "Log in")}
          </button>
        </form>
      </div>
    </dialog>
  );
}
