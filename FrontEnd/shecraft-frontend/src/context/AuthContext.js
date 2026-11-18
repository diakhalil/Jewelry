import { createContext, useContext, useState } from "react";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem("shecraft_user"));
    } catch {
      return null;
    }
  });

  function login(authData) {
    localStorage.setItem("shecraft_user", JSON.stringify(authData));
    if (authData.role === "designer") {
      localStorage.setItem("designerID", authData.user.designerID || authData.user.id);
    } else {
      localStorage.removeItem("designerID");
    }
    setUser(authData);
  }

  function logout() {
    localStorage.removeItem("shecraft_user");
    localStorage.removeItem("authToken");
    localStorage.removeItem("designerID");
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export default function useAuth() {
  return useContext(AuthContext);
}
