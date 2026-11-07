// window.volto belongs in the application adapter.
export const documentation = "window.volto";
export const viewport = () => window.innerWidth;
export const platformAction = (props: { volto: () => void }) => props.volto();
