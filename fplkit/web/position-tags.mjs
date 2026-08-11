/* Detailed positions (winger, full-back, and the rest) that FPL's own
 * element_type does not carry -- it only knows GKP/DEF/MID/FWD, which is not
 * granular enough to tell a left winger from a number ten or a wing-back from
 * a centre-back.
 *
 * There is no free, reliable source for this at Premier League scale: FPL and
 * Understat both only expose the same four-way split, and the one open
 * structured source that claims more (Wikidata's P413) turned out too noisy
 * to trust -- querying it for well-known players like Salah returned
 * contradictory results ("winger", "wing half", even "defender") within the
 * same lookup. Scraping a site whose terms restrict that felt like the wrong
 * trade for this. So this is manual: a best-effort starter set of well-known
 * players below, and a per-player editor in the drawer (the tag chips under a
 * player's name) that writes straight over it, stored in the browser like any
 * other override. Anyone using the board can correct or extend it as they
 * notice something wrong -- multiple tags are expected, since a player can be
 * more than one of these (Saka is both RW and RM, say).
 *
 * Keyed by full_name because fpl_id resets every season and this is meant to
 * survive a rollover; a user's own edits (position-tags.mjs cannot write to
 * disk from the browser) are keyed by id instead, in index.html, and win over
 * whatever is here.
 */

export const POSITION_TAGS = [
  "GK", "CB", "LB", "RB", "WB", "DM", "CM", "LM", "RM", "AM", "LW", "RW", "CF", "ST",
];

export const POSITION_TAG_LABELS = {
  GK: "Goalkeeper", CB: "Centre-back", LB: "Left-back", RB: "Right-back",
  WB: "Wing-back", DM: "Defensive mid", CM: "Central mid", LM: "Left mid",
  RM: "Right mid", AM: "Attacking mid", LW: "Left wing", RW: "Right wing",
  CF: "Second striker", ST: "Striker",
};

// A starter set, not a roster: only well-known, mostly-settled players are
// worth guessing at from outside the club. Everyone else shows their base FPL
// position until someone who actually watches them tags it in the drawer.
export const SEED_TAGS = {
  // Arsenal
  "David Raya": ["GK"], "William Saliba": ["CB"], "Gabriel Magalhães": ["CB"],
  "Jurriën Timber": ["RB", "CB"], "Riccardo Calafiori": ["LB", "CB"],
  "Ben White": ["RB", "CB"], "Declan Rice": ["CM", "DM"],
  "Martin Ødegaard": ["AM", "CM"], "Bukayo Saka": ["RW", "RM"],
  "Gabriel Martinelli": ["LW"], "Leandro Trossard": ["LW", "CF"],
  "Kai Havertz": ["ST", "CF"], "Viktor Gyökeres": ["ST"],
  "Mikel Merino": ["CM", "AM"], "Ethan Nwaneri": ["RW", "AM"],
  // Man City
  "Ederson Santana de Moraes": ["GK"], "Rúben Dias": ["CB"],
  "Joško Gvardiol": ["LB", "CB"], "Nathan Aké": ["CB", "LB"],
  "Kyle Walker": ["RB"], "Rodri": ["DM"], "Mateus Nunes": ["RB", "CM"],
  "Kevin De Bruyne": ["AM", "CM"], "Bernardo Silva": ["AM", "CM"],
  "Phil Foden": ["LW", "AM"], "Jérémy Doku": ["LW", "RW"],
  "Erling Haaland": ["ST"], "Savinho": ["RW"],
  // Liverpool
  "Alisson Ramses Becker": ["GK"], "Virgil van Dijk": ["CB"],
  "Ibrahima Konaté": ["CB"], "Andrew Robertson": ["LB"],
  "Trent Alexander-Arnold": ["RB"], "Conor Bradley": ["RB"],
  "Ryan Gravenberch": ["DM", "CM"], "Alexis Mac Allister": ["CM"],
  "Dominik Szoboszlai": ["RM", "AM"], "Mohamed Salah": ["RW"],
  "Luis Díaz": ["LW"], "Cody Gakpo": ["LW", "ST"], "Darwin Núñez": ["ST"],
  "Florian Wirtz": ["AM", "LW"],
  // Chelsea
  "Robert Sánchez": ["GK"], "Levi Colwill": ["CB", "LB"],
  "Wesley Fofana": ["CB"], "Reece James": ["RB"], "Marc Cucurella": ["LB"],
  "Moisés Caicedo": ["DM"], "Enzo Fernández": ["CM", "DM"],
  "Cole Palmer": ["AM", "RW"], "Pedro Neto": ["RW", "LW"],
  "Noni Madueke": ["RW"], "Nicolas Jackson": ["ST"],
  "Christopher Nkunku": ["CF", "AM"],
  // Man Utd
  "André Onana": ["GK"], "Lisandro Martínez": ["CB"], "Matthijs de Ligt": ["CB"],
  "Noussair Mazraoui": ["RB"], "Diogo Dalot": ["RB", "LB"],
  "Luke Shaw": ["LB"], "Casemiro": ["DM"], "Bruno Fernandes": ["AM", "CM"],
  "Kobbie Mainoo": ["CM"], "Amad Diallo": ["RW"], "Alejandro Garnacho": ["LW"],
  "Rasmus Højlund": ["ST"], "Joshua Zirkzee": ["ST", "CF"],
  "Matheus Cunha": ["CF", "ST"], "Bryan Mbeumo": ["RW"],
  // Tottenham
  "Guglielmo Vicario": ["GK"], "Cristian Romero": ["CB"], "Micky van de Ven": ["CB"],
  "Pedro Porro": ["RB", "RM"], "Destiny Udogie": ["LB"],
  "Yves Bissouma": ["DM"], "Rodrigo Bentancur": ["CM"],
  "James Maddison": ["AM"], "Son Heung-min": ["LW", "ST"],
  "Dejan Kulusevski": ["RW", "AM"], "Dominic Solanke": ["ST"],
  "Brennan Johnson": ["RW"],
  // Newcastle
  "Nick Pope": ["GK"], "Sven Botman": ["CB"], "Fabian Schär": ["CB"],
  "Kieran Trippier": ["RB"], "Dan Burn": ["LB", "CB"],
  "Bruno Guimarães": ["CM", "DM"], "Sandro Tonali": ["CM"],
  "Anthony Gordon": ["LW"], "Jacob Murphy": ["RW"],
  "Alexander Isak": ["ST"], "Harvey Barnes": ["LW"],
  // Aston Villa
  "Emiliano Martínez": ["GK"], "Ezri Konsa": ["CB"], "Pau Torres": ["CB"],
  "Matty Cash": ["RB"], "Lucas Digne": ["LB"], "Boubacar Kamara": ["DM"],
  "Youri Tielemans": ["CM"], "John McGinn": ["CM", "LM"],
  "Morgan Rogers": ["AM", "LW"], "Ollie Watkins": ["ST"],
  "Leon Bailey": ["RW"],
  // Brighton
  "Bart Verbruggen": ["GK"], "Lewis Dunk": ["CB"], "Jan Paul van Hecke": ["CB"],
  "Tariq Lamptey": ["RB", "RM"], "Pervis Estupiñán": ["LB"],
  "Carlos Baleba": ["DM", "CM"], "Kaoru Mitoma": ["LW"],
  "Yankuba Minteh": ["RW"], "João Pedro": ["ST", "CF"],
  "Danny Welbeck": ["ST"],
  // West Ham
  "Alphonse Areola": ["GK"], "Max Kilman": ["CB"], "Jean-Clair Todibo": ["CB"],
  "Vladimír Coufal": ["RB"], "Emerson Palmieri": ["LB"],
  "Guido Rodríguez": ["DM"], "Tomáš Souček": ["CM"],
  "Mohammed Kudus": ["RW", "AM"], "Jarrod Bowen": ["RW", "ST"],
  "Niclas Füllkrug": ["ST"], "Lucas Paquetá": ["AM"],
  // Everton
  "Jordan Pickford": ["GK"], "James Tarkowski": ["CB"], "Jarrad Branthwaite": ["CB"],
  "Vitaliy Mykolenko": ["LB"], "Ashley Young": ["RB"],
  "Idrissa Gueye": ["DM"], "James Garner": ["CM"],
  "Dwight McNeil": ["LW", "LM"], "Iliman Ndiaye": ["AM", "LW"],
  "Dominic Calvert-Lewin": ["ST"], "Beto": ["ST"],
  // Others of note
  "Alex Iwobi": ["AM", "LW"], "Eberechi Eze": ["AM", "LW"],
  "Marc Guéhi": ["CB"], "Adam Wharton": ["CM", "DM"],
  "Jean-Philippe Mateta": ["ST"], "Antoine Semenyo": ["RW", "ST"],
  "Justin Kluivert": ["RW", "LW"], "Evanilson": ["ST"],
  "David Brooks": ["AM", "RW"], "Wilson Odobert": ["LW"],
  "Yoane Wissa": ["ST", "LW"], "Kristoffer Ajer": ["CB", "RB"],
  "Igor Thiago": ["ST"], "Bryan Zaragoza": ["RW"],
  "Nathan Patterson": ["RB"], "Morgan Gibbs-White": ["AM"],
  "Callum Hudson-Odoi": ["LW", "RW"], "Chris Wood": ["ST"],
};
