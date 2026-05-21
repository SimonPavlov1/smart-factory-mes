import { Link as MuiLink, type LinkProps } from "@mui/material";
import { NavLink } from "react-router";

type MenuLinkProps = LinkProps<typeof NavLink> & {
    icon: React.FunctionComponent
};

export const MenuLink = ({ children, icon: Icon, className="", ...props }: MenuLinkProps) => {
    return <MuiLink component={NavLink} className={"menu__nav-link " + className} {...props}>
        <div className="menu__nav-link-content">
            <Icon />
            {children}
        </div>
    </MuiLink>;
}