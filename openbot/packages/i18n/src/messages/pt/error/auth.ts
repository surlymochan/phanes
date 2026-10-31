import type { PartialTranslation } from "../../../message";
import type { messages as source } from "../../en/error/auth";

export const messages = {
  "error.auth.serviceUnavailable":
    "O OpenBot não conseguiu acessar o serviço de contas. Verifique se a API está em execução e tente novamente.",
  "error.auth.networkBlocked":
    "Um firewall ou proxy desta rede impediu o OpenBot de acessar {host}. Peça ao administrador da rede para permitir {host} e tente novamente.",
  "error.auth.signInFirst": "Entre no OpenBot primeiro.",
  "error.auth.signInRequired": "É necessário entrar.",
  "error.auth.accountChangedDuringRegister": "A conta conectada mudou durante o registro deste servidor.",
  "error.auth.hostCredentialUnavailable":
    "A credencial do computador anfitrião remoto está indisponível. Registre o computador anfitrião novamente.",
  "error.auth.codeNotVerified": "Não foi possível verificar o código de login.",
  "error.auth.serviceError": "O serviço de contas retornou um erro.",
  "error.auth.serviceUnreachable":
    "O OpenBot não conseguiu acessar o serviço de contas. Verifique sua conexão e tente novamente.",
  "error.auth.serviceTimeout": "O serviço de contas não respondeu a tempo. Tente novamente.",
  "error.auth.serviceStatus": "O serviço de contas retornou um erro ({status}). Tente novamente mais tarde.",
  "error.auth.invalidHostedServer": "O serviço de contas retornou um servidor hospedado inválido.",
  "error.auth.codeNotSent": "O OpenBot não conseguiu enviar o código de login.",
  "error.auth.deliveryTimeout":
    "O OpenBot não conseguiu confirmar a entrega a tempo. O código ainda pode chegar; verifique a entrega antes de enviar novamente.",
  "error.auth.deliveryInterrupted":
    "A conexão terminou antes de o OpenBot confirmar a entrega. Verifique a entrega para evitar o envio de outro código.",
  "error.auth.deliveryUnknown":
    "O OpenBot não conseguiu confirmar se o código de login foi enviado. Verifique a entrega antes de enviar novamente.",
  "error.auth.adminKeyRequired": "Digite a chave de administração do serviço de contas.",
  "error.auth.adminKeyRejected": "O serviço de contas não aceitou a chave de administração.",
  "error.auth.sessionNotStorable":
    "O OpenBot não consegue guardar a sessão de login neste computador, por isso não pode entrar com uma chave de administração.",
  "error.auth.hostedServerNoAnswer": "O servidor de contas não respondeu no início.",
  "error.auth.hostedServerNoClaim": "O servidor hospedado está desconectado e não tem credencial de acesso.",
  "error.auth.hostedServerClaimMismatch": "A credencial de acesso é para outro servidor hospedado.",
  "error.auth.hostedServerHostMismatch":
    "O computador anfitrião configurado não é o servidor hospedado para o qual esta VM foi criada.",
  "error.auth.hostedServerUnknown": "O servidor de contas não tem nenhum registro deste servidor hospedado.",
  "error.auth.invalidUser": "O serviço de contas retornou um usuário inválido.",
  "error.auth.invalidHostedServerClaim": "Credencial de acesso do servidor hospedado inválida.",
  "error.auth.sessionNotStored": "Não foi possível guardar a sessão.",
} as const satisfies PartialTranslation<typeof source>;
