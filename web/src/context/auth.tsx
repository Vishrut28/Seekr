import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { authRequired, clearToken, getToken, onUnauthorized, setToken } from "../api/client";

interface Auth {
  token: string;
  signIn: (value: string) => void;
  signOut: () => void;
  /** null until the backend has been asked whether it wants a token */
  required: boolean | null;
}

const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState(getToken);
  // A deployment with RIP_API_TOKEN unset needs no sign-in, and asking for a
  // token it would ignore is a door with no lock in front of an open room.
  const [required, setRequired] = useState<boolean | null>(null);

  useEffect(() => {
    let live = true;
    authRequired().then((value) => live && setRequired(value));
    return () => {
      live = false;
    };
  }, []);

  // A rejected request anywhere in the app drops straight back to the gate —
  // an expired token should not leave five panels showing "unauthorized".
  useEffect(
    () =>
      onUnauthorized(() => {
        clearToken();
        setTokenState("");
      }),
    [],
  );

  const signIn = useCallback((value: string) => {
    setToken(value);
    setTokenState(value.trim());
  }, []);

  const signOut = useCallback(() => {
    clearToken();
    setTokenState("");
  }, []);

  const value = useMemo(
    () => ({ token, signIn, signOut, required }),
    [token, signIn, signOut, required],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
