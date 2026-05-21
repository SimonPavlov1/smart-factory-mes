// router.tsx
import { createBrowserRouter, Outlet, redirect } from "react-router";
import { Box } from "@mui/material";
import { AuthPage } from "./pages/AuthPage";
import { RequestsPage } from "./pages/RequestsPage/RequestsPage";
import { StaffPage } from "./pages/StaffPage/StaffPage";
import { PanelsPage } from "./pages/PanelsPage";
import { NavMenu } from "./components/NavMenu/NavMenu";
import { getToken } from "./utils/token";

export default createBrowserRouter([
  {
    id: "root",
    path: "/",
    element: (
        <Box sx={{ display: "flex", height: "100vh" }}>
          <NavMenu />
          <Box sx={{ padding: "25px 40px", flex: 1, overflow: "auto" }}>
            <Outlet />
          </Box>
        </Box>
    ),
    loader: () => {
      console.log(getToken("access"))
      if (!getToken("access")) {
        throw redirect('/auth');
      }
    },
    children: [
      { path: "requests", Component: RequestsPage },
      { index: true, Component: PanelsPage },
      { path: "staff", Component: StaffPage }
    ],
  },
  { path: "/auth", element: <AuthPage /> },
]);